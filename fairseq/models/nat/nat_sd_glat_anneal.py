# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from json import decoder
import torch
import torch.nn.functional as F
from fairseq import utils
from fairseq.iterative_refinement_generator import DecoderOut
from fairseq.models import register_model, register_model_architecture
from fairseq.models.nat import FairseqNATSharedDecoder, FairseqNATModel, ensemble_decoder
from fairseq.models.transformer import Embedding
from fairseq.modules.transformer_sentence_encoder import init_bert_params
from fairseq.models.fairseq_decoder import FairseqDecoder
import torch
import logging
# from .nat_sd_shared import NATransformerDecoder

logger = logging.getLogger(__name__)

def _mean_pooling(enc_feats, src_masks):
    # enc_feats: T x B x C
    # src_masks: B x T or None
    if src_masks is None:
        enc_feats = enc_feats.mean(0)
    else:
        src_masks = (~src_masks).transpose(0, 1).type_as(enc_feats)
        enc_feats = (
            (enc_feats / src_masks.sum(0)[None, :, None]) * src_masks[:, :, None]
        ).sum(0)
    return enc_feats


def _argmax(x, dim):
    return (x == x.max(dim, keepdim=True)[0]).type_as(x)


def _uniform_assignment(src_lens, trg_lens):
    max_trg_len = trg_lens.max()
    steps = (src_lens.float() - 1) / (trg_lens.float() - 1)  # step-size
    # max_trg_len
    index_t = utils.new_arange(trg_lens, max_trg_len).float()
    index_t = steps[:, None] * index_t[None, :]  # batch_size X max_trg_len
    index_t = torch.round(index_t).long().detach()
    return index_t


@register_model("nat_sd_glat_anneal")
class NATransformerModel(FairseqNATModel):
    def __init__(self, args, encoder, decoder):
        super().__init__(args, encoder, decoder)
        self.inference_decoder_layer = getattr(args, 'inference_decoder_layer', -1)

    @property
    def allow_length_beam(self):
        return True

    @staticmethod
    def add_args(parser):
        FairseqNATModel.add_args(parser)

        # length prediction
        parser.add_argument(
            "--src-embedding-copy",
            action="store_true",
            help="copy encoder word embeddings as the initial input of the decoder",
        )
        parser.add_argument(
            "--pred-length-offset",
            action="store_true",
            help="predicting the length difference between the target and source sentences",
        )
        parser.add_argument(
            "--sg-length-pred",
            action="store_true",
            help="stop the gradients back-propagated from the length predictor",
        )
        parser.add_argument(
            "--length-loss-factor",
            type=float,
            help="weights on the length prediction loss",
        )
        parser.add_argument(
            "--restore-decoder-from",
            default="off",
            action="store",
        )

        parser.add_argument(
            '--hard-argmax',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--yhat-temp',
            type=float,
            default=0.1
        )
        parser.add_argument(
            '--share-ffn',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--share-attn',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--inference-decoder-layer',
            type=int,
            default=-1
        )
        parser.add_argument(
            '--fix-schedule',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--always-half',
            action='store_true',
            default=False
        )


    @classmethod
    def build_decoder(cls, args, tgt_dict, embed_tokens):
        decoder = NATransformerDecoder(args, tgt_dict, embed_tokens)
        if getattr(args, "apply_bert_init", False):
            decoder.apply(init_bert_params)
        return decoder

    def forward(
        self, src_tokens, src_lengths, prev_output_tokens, tgt_tokens, glat=None, train_ratio=None, **kwargs
    ):
        # encoding
        encoder_out = self.encoder(src_tokens, src_lengths=src_lengths, **kwargs)

        # length prediction
        length_out = self.decoder.forward_length(
            normalize=False, encoder_out=encoder_out
        )
        length_tgt = self.decoder.forward_length_prediction(
            length_out, encoder_out, tgt_tokens
        )

        tgt_mask = tgt_tokens.ne(self.pad)

        # decoding
        anneal_info = None
        glat_info = None
        if glat and tgt_tokens is not None:
            if "context_p" in glat:
                with torch.no_grad():
                    # 1. calculate glancing probability
                    word_ins_out_list = self.decoder(
                        normalize=False,
                        prev_output_tokens=prev_output_tokens,
                        encoder_out=encoder_out,
                    )
                    pred_tokens = word_ins_out_list[-1].argmax(-1)
                    nonpad_positions = ~tgt_tokens.eq(self.pad)
                    same_num = ((pred_tokens == tgt_tokens) & nonpad_positions).sum(1)
                    seq_lens = (nonpad_positions).sum(1)
                    keep_prob = ((seq_lens - same_num) / seq_lens * glat['context_p']).unsqueeze(-1)
                    # keep: True, drop: False
                    keep_word_mask = (torch.rand(prev_output_tokens.shape, device=pred_tokens.device) < keep_prob).bool()
                    glat_prev_output_tokens = prev_output_tokens.masked_fill(keep_word_mask, 0) + tgt_tokens.masked_fill(~keep_word_mask, 0)
                    glat_tgt_tokens = tgt_tokens.masked_fill(keep_word_mask, self.pad)

                    prev_output_tokens, tgt_tokens = glat_prev_output_tokens, glat_tgt_tokens
                    glat_info = {
                        "glat_accu": (same_num.sum() / seq_lens.sum()).item(),
                        "glat_context_p": glat['context_p'],
                        "glat_keep": keep_prob.mean().item()
                    }

                    # 2. get per layer accuracy
                    all_layer_acc_list = []
                    for per_layer_output_logits in word_ins_out_list:
                        per_layer_acc = torch.div(torch.sum((per_layer_output_logits.argmax(-1) == tgt_tokens) & tgt_mask, dim=-1, dtype=per_layer_output_logits.dtype),
                                                  torch.sum(tgt_mask, dim=-1))
                        all_layer_acc_list.append(per_layer_acc)

                    # 2.1 calculate per layer y_hat weights
                    if self.args.fix_schedule:
                        all_layer_weight = [torch.ones((per_layer_output_logits.size(0), ),
                                            dtype=per_layer_output_logits.dtype,
                                            device=per_layer_output_logits.device) * min(0.5, train_ratio * 10.0)
                                            for _ in range(len(word_ins_out_list))]
                    elif self.args.always_half:
                        all_layer_weight = [torch.ones((per_layer_output_logits.size(0),),
                                                       dtype=per_layer_output_logits.dtype,
                                                       device=per_layer_output_logits.device) * 0.5
                                            for _ in range(len(word_ins_out_list))]
                    else:
                        # Note: attempt 1,  p = acc ** alpha / 2,  max p is 0.5
                        all_layer_weight = [per_layer_acc ** 0.5 / 2 for per_layer_acc in all_layer_acc_list]
                    anneal_info = {
                        "glat_anneal": [torch.mean(x).item() for x in all_layer_acc_list]
                    }

            elif "schedule" in glat:
                raise NotImplementedError
                # schedule = glat["schedule"]
                # keep_word_mask = (torch.rand(prev_output_tokens.shape, device=length_out.device) < schedule).bool()
                # glat_prev_output_tokens = prev_output_tokens.masked_fill(keep_word_mask, 0) + tgt_tokens.masked_fill(~keep_word_mask, 0)
                # glat_tgt_tokens = tgt_tokens.masked_fill(keep_word_mask, self.pad)
                #
                # prev_output_tokens, tgt_tokens = glat_prev_output_tokens, glat_tgt_tokens
                # glat_info = {
                #     "glat_keep": schedule
                # }



        if self.training:
            word_ins_out_list = self.decoder(
                normalize=False,
                prev_output_tokens=prev_output_tokens,
                encoder_out=encoder_out,
                feed_forward_weights=all_layer_weight
            )
        else:
            word_ins_out_list = self.decoder(
                normalize=False,
                prev_output_tokens=prev_output_tokens,
                encoder_out=encoder_out,
            )

        ret_val = {
            "length": {
                "out": length_out,
                "tgt": length_tgt,
                "factor": self.decoder.length_loss_factor,
            },
        }

        for _idx, word_ins_out in enumerate(word_ins_out_list):
            ret_val[f"word_ins_{_idx}"] = {
                "out": word_ins_out,
                "tgt": tgt_tokens,
                "mask": tgt_mask,
                "ls": self.args.label_smoothing,
                "nll_loss": True,
                "factor": 1 / self.decoder.num_layers,
            }

        if glat_info is not None:
            ret_val.update(glat_info)

        if anneal_info is not None:
            ret_val.update(anneal_info)
        return ret_val

    def forward_decoder(self, decoder_out, encoder_out, decoding_format=None, **kwargs):
        step = decoder_out.step
        output_tokens = decoder_out.output_tokens
        output_scores = decoder_out.output_scores
        history = decoder_out.history

        # execute the decoder
        output_masks = output_tokens.ne(self.pad)
        output_logits_list = self.decoder(
            normalize=True,
            prev_output_tokens=output_tokens,
            encoder_out=encoder_out,
            step=step,
        )

        inference_decoder_layer = self.inference_decoder_layer

        output_logits = output_logits_list[inference_decoder_layer]  # take the last layer by default

        _scores, _tokens = output_logits.max(-1)

        output_tokens.masked_scatter_(output_masks, _tokens[output_masks])
        output_scores.masked_scatter_(output_masks, _scores[output_masks])
        if history is not None:
            history.append(output_tokens.clone())

        return decoder_out._replace(
            output_tokens=output_tokens,
            output_scores=output_scores,
            attn=None,
            history=history,
        )

    def initialize_output_tokens(self, encoder_out, src_tokens):
        # length prediction
        length_tgt = self.decoder.forward_length_prediction(
            self.decoder.forward_length(normalize=True, encoder_out=encoder_out),
            encoder_out=encoder_out,
        )

        max_length = length_tgt.clamp_(min=2).max()
        idx_length = utils.new_arange(src_tokens, max_length)

        initial_output_tokens = src_tokens.new_zeros(
            src_tokens.size(0), max_length
        ).fill_(self.pad)
        initial_output_tokens.masked_fill_(
            idx_length[None, :] < length_tgt[:, None], self.unk
        )
        initial_output_tokens[:, 0] = self.bos
        initial_output_tokens.scatter_(1, length_tgt[:, None] - 1, self.eos)

        initial_output_scores = initial_output_tokens.new_zeros(
            *initial_output_tokens.size()
        ).type_as(encoder_out["encoder_out"][0])

        return DecoderOut(
            output_tokens=initial_output_tokens,
            output_scores=initial_output_scores,
            attn=None,
            step=0,
            max_step=0,
            history=None,
        )

    def regenerate_length_beam(self, decoder_out, beam_size):
        output_tokens = decoder_out.output_tokens
        length_tgt = output_tokens.ne(self.pad).sum(1)
        length_tgt = (
            length_tgt[:, None]
            + utils.new_arange(length_tgt, 1, beam_size)
            - beam_size // 2
        )
        length_tgt = length_tgt.view(-1).clamp_(min=2)
        max_length = length_tgt.max()
        idx_length = utils.new_arange(length_tgt, max_length)

        initial_output_tokens = output_tokens.new_zeros(
            length_tgt.size(0), max_length
        ).fill_(self.pad)
        initial_output_tokens.masked_fill_(
            idx_length[None, :] < length_tgt[:, None], self.unk
        )
        initial_output_tokens[:, 0] = self.bos
        initial_output_tokens.scatter_(1, length_tgt[:, None] - 1, self.eos)

        initial_output_scores = initial_output_tokens.new_zeros(
            *initial_output_tokens.size()
        ).type_as(decoder_out.output_scores)

        return decoder_out._replace(
            output_tokens=initial_output_tokens, output_scores=initial_output_scores
        )


class NATransformerDecoder(FairseqNATSharedDecoder):
    def __init__(self, args, dictionary, embed_tokens, no_encoder_attn=False):
        super().__init__(
            args, dictionary, embed_tokens, no_encoder_attn=no_encoder_attn
        )
        self.dictionary = dictionary
        self.bos = dictionary.bos()
        self.unk = dictionary.unk()
        self.eos = dictionary.eos()

        self.encoder_embed_dim = args.encoder_embed_dim
        self.sg_length_pred = getattr(args, "sg_length_pred", False)
        self.pred_length_offset = getattr(args, "pred_length_offset", False)
        self.length_loss_factor = getattr(args, "length_loss_factor", 0.1)
        self.src_embedding_copy = getattr(args, "src_embedding_copy", False)
        self.embed_length = Embedding(256, self.encoder_embed_dim, None)

    @ensemble_decoder
    def forward(self, normalize, encoder_out, prev_output_tokens, step=0, feed_forward_weights=None, **unused):
        _, all_features = self.extract_features(
            prev_output_tokens,
            encoder_out=encoder_out,
            embedding_copy=(step == 0) & self.src_embedding_copy,
            feed_forward_weights=feed_forward_weights
        )
        all_layer_output_logits = all_features['all_layer_output_logits']
        return [F.log_softmax(x.transpose(0, 1), -1) if normalize else x.transpose(0, 1)
                for x in all_layer_output_logits]

    @ensemble_decoder
    def forward_length(self, normalize, encoder_out):
        enc_feats = encoder_out["encoder_out"][0]  # T x B x C
        if len(encoder_out["encoder_padding_mask"]) > 0:
            src_masks = encoder_out["encoder_padding_mask"][0]  # B x T
        else:
            src_masks = None
        enc_feats = _mean_pooling(enc_feats, src_masks)
        if self.sg_length_pred:
            enc_feats = enc_feats.detach()
        length_out = F.linear(enc_feats, self.embed_length.weight)
        return F.log_softmax(length_out, -1) if normalize else length_out

    def extract_features(
            self,
            prev_output_tokens,
            encoder_out=None,
            early_exit=None,
            embedding_copy=False,
            feed_forward_weights=None,
            **unused
    ):
        """
        Similar to *forward* but only return features.

        Inputs:
            prev_output_tokens: Tensor(B, T)
            encoder_out: a dictionary of hidden states and masks

        Returns:
            tuple:
                - the decoder's features of shape `(batch, tgt_len, embed_dim)`
                - a dictionary with any model-specific outputs
            the LevenshteinTransformer decoder has full-attention to all generated tokens
        """
        # embedding
        if embedding_copy:
            src_embd = encoder_out["encoder_embedding"][0]
            if len(encoder_out["encoder_padding_mask"]) > 0:
                src_mask = encoder_out["encoder_padding_mask"][0]
            else:
                src_mask = None
            src_mask = (
                ~src_mask
                if src_mask is not None
                else prev_output_tokens.new_ones(*src_embd.size()[:2]).bool()
            )

            x, decoder_padding_mask = self.forward_embedding(
                prev_output_tokens,
                self.forward_copying_source(
                    src_embd, src_mask, prev_output_tokens.ne(self.padding_idx)
                ),
            )

        else:
            x, decoder_padding_mask = self.forward_embedding(prev_output_tokens)

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)
        attn = None
        inner_states = [x]
        # layer_output_list = []
        # layer_out = torch.zeros(x.size()).to(x)
        # decoder layers
        all_layer_output_logits = []
        temperature = self.args.yhat_temp
        for i, layer in enumerate(self.layers):
            # early exit from the decoder.
            if (early_exit is not None) and (i >= early_exit):
                break
            layer_out_logits = self.output_layer(x)
            if not self.args.hard_argmax:
                # soft argmax
                y_hat = torch.matmul(
                    torch.softmax(layer_out_logits.detach() / temperature, dim=-1).view(-1, layer_out_logits.size(-1)),
                    self.embed_tokens.weight).view(x.size())
            else:
                # hard argmax
                y_hat = self.embed_tokens(layer_out_logits.argmax(dim=-1))
            if i > 0:
                all_layer_output_logits.append(layer_out_logits)
            if feed_forward_weights is None:
                new_x = (x + y_hat) / torch.sqrt(torch.tensor(2.))  # affect how to decode
            else:
                p = feed_forward_weights[i].unsqueeze(0).unsqueeze(2)
                one_minus_p = (1 - p)
                new_x = torch.div((p * x + one_minus_p * y_hat), torch.sqrt(torch.square(p) + torch.square(one_minus_p))) # (p * x + one_minus_p * y_hat)
            # new_x = (x + layer_out) / torch.sqrt(torch.tensor(2.))
            x, attn, _ = layer(
                new_x,
                encoder_out["encoder_out"][0]
                if (encoder_out is not None and len(encoder_out["encoder_out"]) > 0)
                else None,
                encoder_out["encoder_padding_mask"][0]
                if (
                        encoder_out is not None
                        and len(encoder_out["encoder_padding_mask"]) > 0
                )
                else None,
                self_attn_mask=None,
                self_attn_padding_mask=decoder_padding_mask,
            )
            inner_states.append(x)

        all_layer_output_logits.append(self.output_layer(x))
        if self.layer_norm:
            x = self.layer_norm(x)

        # T x B x C -> B x T x C
        x = x.transpose(0, 1)

        if self.project_out_dim is not None:
            x = self.project_out_dim(x)

        return x, {"attn": attn, "inner_states": inner_states, "all_layer_output_logits": all_layer_output_logits}

    def forward_embedding(self, prev_output_tokens, states=None):
        # embed positions
        positions = (
            self.embed_positions(prev_output_tokens)
            if self.embed_positions is not None
            else None
        )

        # embed tokens and positions
        if states is None:
            x = self.embed_scale * self.embed_tokens(prev_output_tokens)
            if self.project_in_dim is not None:
                x = self.project_in_dim(x)
        else:
            x = states

        if positions is not None:
            x += positions
        x = self.dropout_module(x)
        decoder_padding_mask = prev_output_tokens.eq(self.padding_idx)
        return x, decoder_padding_mask

    def forward_copying_source(self, src_embeds, src_masks, tgt_masks):
        length_sources = src_masks.sum(1)
        length_targets = tgt_masks.sum(1)
        mapped_inputs = _uniform_assignment(length_sources, length_targets).masked_fill(
            ~tgt_masks, 0
        )
        copied_embedding = torch.gather(
            src_embeds,
            1,
            mapped_inputs.unsqueeze(-1).expand(
                *mapped_inputs.size(), src_embeds.size(-1)
            ),
        )
        return copied_embedding

    def forward_length_prediction(self, length_out, encoder_out, tgt_tokens=None):
        enc_feats = encoder_out["encoder_out"][0]  # T x B x C
        if len(encoder_out["encoder_padding_mask"]) > 0:
            src_masks = encoder_out["encoder_padding_mask"][0]  # B x T
        else:
            src_masks = None
        if self.pred_length_offset:
            if src_masks is None:
                src_lengs = enc_feats.new_ones(enc_feats.size(1)).fill_(
                    enc_feats.size(0)
                )
            else:
                src_lengs = (~src_masks).transpose(0, 1).type_as(enc_feats).sum(0)
            src_lengs = src_lengs.long()

        if tgt_tokens is not None:
            # obtain the length target
            tgt_lengs = tgt_tokens.ne(self.padding_idx).sum(1).long()
            if self.pred_length_offset:
                length_tgt = tgt_lengs - src_lengs + 128
            else:
                length_tgt = tgt_lengs
            length_tgt = length_tgt.clamp(min=0, max=255)

        else:
            # predict the length target (greedy for now)
            # TODO: implementing length-beam
            pred_lengs = length_out.max(-1)[1]
            if self.pred_length_offset:
                length_tgt = pred_lengs - 128 + src_lengs
            else:
                length_tgt = pred_lengs

        return length_tgt


@register_model_architecture(
    "nat_sd_glat_anneal", "nat_sd_glat_anneal"
)
def base_architecture(args):
    args.encoder_embed_path = getattr(args, "encoder_embed_path", None)
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_ffn_embed_dim = getattr(args, "encoder_ffn_embed_dim", 2048)
    args.encoder_layers = getattr(args, "encoder_layers", 6)
    args.encoder_attention_heads = getattr(args, "encoder_attention_heads", 8)
    args.encoder_normalize_before = getattr(args, "encoder_normalize_before", False)
    args.encoder_learned_pos = getattr(args, "encoder_learned_pos", False)
    args.decoder_embed_path = getattr(args, "decoder_embed_path", None)
    args.decoder_embed_dim = getattr(args, "decoder_embed_dim", args.encoder_embed_dim)
    args.decoder_ffn_embed_dim = getattr(
        args, "decoder_ffn_embed_dim", args.encoder_ffn_embed_dim
    )
    args.decoder_layers = getattr(args, "decoder_layers", 6)
    args.decoder_attention_heads = getattr(args, "decoder_attention_heads", 8)
    args.decoder_normalize_before = getattr(args, "decoder_normalize_before", False)
    args.decoder_learned_pos = getattr(args, "decoder_learned_pos", False)
    args.attention_dropout = getattr(args, "attention_dropout", 0.0)
    args.activation_dropout = getattr(args, "activation_dropout", 0.0)
    args.activation_fn = getattr(args, "activation_fn", "relu")
    args.dropout = getattr(args, "dropout", 0.1)
    args.adaptive_softmax_cutoff = getattr(args, "adaptive_softmax_cutoff", None)
    args.adaptive_softmax_dropout = getattr(args, "adaptive_softmax_dropout", 0)
    args.share_decoder_input_output_embed = getattr(
        args, "share_decoder_input_output_embed", False
    )
    args.share_all_embeddings = getattr(args, "share_all_embeddings", False)
    args.no_token_positional_embeddings = getattr(
        args, "no_token_positional_embeddings", False
    )
    args.adaptive_input = getattr(args, "adaptive_input", False)
    args.apply_bert_init = getattr(args, "apply_bert_init", False)

    args.decoder_output_dim = getattr(
        args, "decoder_output_dim", args.decoder_embed_dim
    )
    args.decoder_input_dim = getattr(args, "decoder_input_dim", args.decoder_embed_dim)

    # --- special arguments ---
    args.sg_length_pred = getattr(args, "sg_length_pred", False)
    args.pred_length_offset = getattr(args, "pred_length_offset", False)
    args.length_loss_factor = getattr(args, "length_loss_factor", 0.1)
    args.src_embedding_copy = getattr(args, "src_embedding_copy", False)


@register_model_architecture(
    "nat_sd_glat_anneal", "nat_sd_glat_anneal_12d"
)
def base_architecture_12d(args):
    args.encoder_embed_path = getattr(args, "encoder_embed_path", None)
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_ffn_embed_dim = getattr(args, "encoder_ffn_embed_dim", 2048)
    args.encoder_layers = getattr(args, "encoder_layers", 6)
    args.encoder_attention_heads = getattr(args, "encoder_attention_heads", 8)
    args.encoder_normalize_before = getattr(args, "encoder_normalize_before", False)
    args.encoder_learned_pos = getattr(args, "encoder_learned_pos", False)
    args.decoder_embed_path = getattr(args, "decoder_embed_path", None)
    args.decoder_embed_dim = getattr(args, "decoder_embed_dim", args.encoder_embed_dim)
    args.decoder_ffn_embed_dim = getattr(
        args, "decoder_ffn_embed_dim", args.encoder_ffn_embed_dim
    )
    args.decoder_layers = getattr(args, "decoder_layers", 12)
    args.decoder_attention_heads = getattr(args, "decoder_attention_heads", 8)
    args.decoder_normalize_before = getattr(args, "decoder_normalize_before", False)
    args.decoder_learned_pos = getattr(args, "decoder_learned_pos", False)
    args.attention_dropout = getattr(args, "attention_dropout", 0.0)
    args.activation_dropout = getattr(args, "activation_dropout", 0.0)
    args.activation_fn = getattr(args, "activation_fn", "relu")
    args.dropout = getattr(args, "dropout", 0.1)
    args.adaptive_softmax_cutoff = getattr(args, "adaptive_softmax_cutoff", None)
    args.adaptive_softmax_dropout = getattr(args, "adaptive_softmax_dropout", 0)
    args.share_decoder_input_output_embed = getattr(
        args, "share_decoder_input_output_embed", False
    )
    args.share_all_embeddings = getattr(args, "share_all_embeddings", False)
    args.no_token_positional_embeddings = getattr(
        args, "no_token_positional_embeddings", False
    )
    args.adaptive_input = getattr(args, "adaptive_input", False)
    args.apply_bert_init = getattr(args, "apply_bert_init", False)

    args.decoder_output_dim = getattr(
        args, "decoder_output_dim", args.decoder_embed_dim
    )
    args.decoder_input_dim = getattr(args, "decoder_input_dim", args.decoder_embed_dim)

    # --- special arguments ---
    args.sg_length_pred = getattr(args, "sg_length_pred", False)
    args.pred_length_offset = getattr(args, "pred_length_offset", False)
    args.length_loss_factor = getattr(args, "length_loss_factor", 0.1)
    args.src_embedding_copy = getattr(args, "src_embedding_copy", False)


@register_model_architecture(
    "nat_sd_glat_anneal", "nat_sd_glat_anneal_24d"
)
def base_architecture_24d(args):
    args.encoder_embed_path = getattr(args, "encoder_embed_path", None)
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_ffn_embed_dim = getattr(args, "encoder_ffn_embed_dim", 2048)
    args.encoder_layers = getattr(args, "encoder_layers", 6)
    args.encoder_attention_heads = getattr(args, "encoder_attention_heads", 8)
    args.encoder_normalize_before = getattr(args, "encoder_normalize_before", False)
    args.encoder_learned_pos = getattr(args, "encoder_learned_pos", False)
    args.decoder_embed_path = getattr(args, "decoder_embed_path", None)
    args.decoder_embed_dim = getattr(args, "decoder_embed_dim", args.encoder_embed_dim)
    args.decoder_ffn_embed_dim = getattr(
        args, "decoder_ffn_embed_dim", args.encoder_ffn_embed_dim
    )
    args.decoder_layers = getattr(args, "decoder_layers", 24)
    args.decoder_attention_heads = getattr(args, "decoder_attention_heads", 8)
    args.decoder_normalize_before = getattr(args, "decoder_normalize_before", False)
    args.decoder_learned_pos = getattr(args, "decoder_learned_pos", False)
    args.attention_dropout = getattr(args, "attention_dropout", 0.0)
    args.activation_dropout = getattr(args, "activation_dropout", 0.0)
    args.activation_fn = getattr(args, "activation_fn", "relu")
    args.dropout = getattr(args, "dropout", 0.1)
    args.adaptive_softmax_cutoff = getattr(args, "adaptive_softmax_cutoff", None)
    args.adaptive_softmax_dropout = getattr(args, "adaptive_softmax_dropout", 0)
    args.share_decoder_input_output_embed = getattr(
        args, "share_decoder_input_output_embed", False
    )
    args.share_all_embeddings = getattr(args, "share_all_embeddings", False)
    args.no_token_positional_embeddings = getattr(
        args, "no_token_positional_embeddings", False
    )
    args.adaptive_input = getattr(args, "adaptive_input", False)
    args.apply_bert_init = getattr(args, "apply_bert_init", False)

    args.decoder_output_dim = getattr(
        args, "decoder_output_dim", args.decoder_embed_dim
    )
    args.decoder_input_dim = getattr(args, "decoder_input_dim", args.decoder_embed_dim)

    # --- special arguments ---
    args.sg_length_pred = getattr(args, "sg_length_pred", False)
    args.pred_length_offset = getattr(args, "pred_length_offset", False)
    args.length_loss_factor = getattr(args, "length_loss_factor", 0.1)
    args.src_embedding_copy = getattr(args, "src_embedding_copy", False)

@register_model_architecture(
    "nat_sd_glat_anneal", "nat_sd_glat_anneal_12e"
)
def big_architecture_12e(args):
    args.encoder_layers = getattr(args, "encoder_layers", 12)
    base_architecture(args)
