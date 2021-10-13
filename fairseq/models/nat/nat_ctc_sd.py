# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import torch
import torch.nn.functional as F
from fairseq import utils
from fairseq.iterative_refinement_generator import DecoderOut
from fairseq.models import register_model, register_model_architecture
from fairseq.models.nat import FairseqNATSharedDecoder, FairseqNATModel, ensemble_decoder
from fairseq.models.transformer import Embedding
from fairseq.modules.transformer_sentence_encoder import init_bert_params
from typing import Union
import logging
import random, math
from .nat_sd_shared import NATransformerDecoder

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


def _gumbel_softmax(logits, temperature=1.0, withnoise=True, hard=True, eps=1e-10):
    def sample_gumbel(shape, eps=1e-10):
        U = torch.rand(shape).cuda()
        return -torch.log(-torch.log(U + eps) + eps)

    if withnoise:
        gumbels = sample_gumbel(logits.size())
        y_soft = ((logits + gumbels) * 1.0 / temperature).softmax(2)
    else:
        y_soft = (logits * 1.0 / temperature).softmax(2)

    index = y_soft.max(dim=-1, keepdim=True)[1]
    y_hard = torch.zeros_like(logits).scatter_(2, index, 1.0)
    ret = (y_hard - y_soft).detach() + y_soft
    if hard:
        return ret
    else:
        return y_soft


@register_model("nat_ctc_sd")
class NATransformerModel(FairseqNATModel):
    def __init__(self, args, encoder, decoder):
        super().__init__(args, encoder, decoder)
        from ctcdecode import CTCBeamDecoder
        self.ctc_decoder = CTCBeamDecoder(
            decoder.dictionary.symbols,
            model_path=None,
            alpha=0,
            beta=0,
            cutoff_top_n=40,
            cutoff_prob=1.0,
            beam_width=args.ctc_beam_size,
            num_processes=20,
            blank_id=decoder.dictionary.blank_index,
            log_probs_input=False
        )
        self.inference_decoder_layer = getattr(args, 'inference_decoder_layer', -1)
        self.plain_ctc = getattr(args, 'plain_ctc', False)

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
            "--src-upsample-scale",
            type=int,
            default=1
        )
        parser.add_argument(
            '--use-ctc-decoder',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--ctc-beam-size',
            default=1,
            type=int
        )
        parser.add_argument(
            '--ctc-beam-size-train',
            default=1,
            type=int
        )
        parser.add_argument(
            '--num-cross-layer-sample',
            default=0,
            type=int
        )
        # parser.add_argument(
        #     '--hard-argmax',
        #     action='store_true',
        #     default=False
        # )
        # parser.add_argument(
        #     '--yhat-temp',
        #     type=float,
        #     default=0.1
        # )
        parser.add_argument(
            '--inference-decoder-layer',
            type=int,
            default=-1
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
            '--sample-option',
            type=str,
            default='hard'
        )
        parser.add_argument(
            '--softmax-temp',
            type=float,
            default=1
        )
        parser.add_argument(
            '--temp-anneal',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--num-topk',
            default=1,
            type=int
        )
        parser.add_argument(
            '--copy-src-token',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--force-detach',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--softcopy',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--softcopy-temp',
            default=5,
            type=float
        )
        parser.add_argument(
            '--concat-yhat',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--concat-dropout',
            type=float,
            default=0
        )
        parser.add_argument(
            '--layer-drop-ratio',
            type=float,
            default=0.0
        )
        parser.add_argument(
            '--all-layer-drop',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--yhat-posemb',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--dropout-anneal',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--dropout-anneal-end-ratio',
            type=float,
            default=0
        )
        parser.add_argument(
            '--force-ls',
            action='store_true',
            default=False
        )
        parser.add_argument(
            '--repeat-layer',
            type=int,
            default=0
        )

    @classmethod
    def build_decoder(cls, args, tgt_dict, embed_tokens):
        decoder = NATransformerDecoder(args, tgt_dict, embed_tokens)
        decoder.repeat_layer = getattr(args, 'repeat_layer', 0)
        if getattr(args, "apply_bert_init", False):
            decoder.apply(init_bert_params)
        return decoder

    def sequence_ctc_loss_with_logits(self,
                                      logits: torch.FloatTensor,
                                      logit_mask: Union[torch.FloatTensor, torch.BoolTensor],
                                      targets: torch.LongTensor,
                                      target_mask: Union[torch.FloatTensor, torch.BoolTensor],
                                      blank_index: torch.LongTensor,
                                      label_smoothing=0,
                                      reduce=True
                                      ) -> torch.FloatTensor:
        # # lengths : (batch_size, )
        # if self.args.force_ls:  # NOTE temp fix, to really try ls without mess up previous exps
        #     label_smoothing = self.args.label_smoothing
        # else:
        #     label_smoothing = label_smoothing
        # calculated by counting number of mask
        logit_lengths = (logit_mask.bool()).long().sum(1)

        if len(targets.size()) == 1:
            targets = targets.unsqueeze(0)
            target_mask = target_mask.unsqueeze(0)
        target_lengths = (target_mask.bool()).long().sum(1)

        # (batch_size, T, n_class)
        log_probs = logits.log_softmax(-1)
        # log_probs_T : (T, batch_size, n_class), this kind of shape is required for ctc_loss
        log_probs_T = log_probs.transpose(0, 1)
        #     assert (target_lengths == 0).any()
        targets = targets.long()
        targets = targets[target_mask.bool()]
        if reduce:
            loss = F.ctc_loss(
                log_probs_T.float(),  # compatible with fp16
                targets,
                logit_lengths,
                target_lengths,
                blank=blank_index,
                reduction="mean",
                zero_infinity=True,
            )
        else:
            loss = F.ctc_loss(
                log_probs_T.float(),  # compatible with fp16
                targets,
                logit_lengths,
                target_lengths,
                blank=blank_index,
                reduction="none",
                zero_infinity=True,
            )
            loss = torch.stack([a / b for a, b in zip(loss, target_lengths)])

        n_invalid_samples = (logit_lengths < target_lengths).long().sum()

        if n_invalid_samples > 0:
            logger.warning(
                f"The length of predicted alignment is shoter than target length, increase upsample factor: {n_invalid_samples} samples"
            )
            # raise ValueError

        if label_smoothing > 0:
            smoothed_loss = -log_probs.mean(-1)[logit_mask.bool()].mean()
            loss = (1 - label_smoothing) * loss + label_smoothing * smoothed_loss
        return loss

    def forward(
            self, src_tokens, src_lengths, prev_output_tokens, tgt_tokens, reduce=True, train_ratio=None, **kwargs
    ):
        if train_ratio is not None:
            self.encoder.train_ratio = train_ratio
            self.decoder.train_ratio = train_ratio

        prev_output_tokens = self.initialize_output_tokens_by_src_tokens(src_tokens)
        prev_output_tokens_mask = prev_output_tokens.ne(self.pad)

        # encoding
        encoder_out = self.encoder(src_tokens, src_lengths=src_lengths, **kwargs)

        # decoding
        output_logits_list = self.decoder(
            normalize=False,
            prev_output_tokens=prev_output_tokens,
            encoder_out=encoder_out,
            train_ratio=train_ratio
        )
        target_mask = tgt_tokens.ne(self.pad)

        if self.args.num_cross_layer_sample != 0:
            output_logits_list = torch.stack(output_logits_list, dim=0)

            N_SAMPLE = self.args.num_cross_layer_sample

            num_decoder_layer = output_logits_list.size(0)
            num_tokens = prev_output_tokens.size(1)
            num_vocab = output_logits_list.size(-1)
            batch_size = prev_output_tokens.size(0)

            all_sample_ctc_loss = 0

            for sample_id in range(N_SAMPLE):
                cross_layer_sampled_ids_ts = torch.randint(num_decoder_layer, (batch_size * num_tokens,), device=output_logits_list.device)
                output_logits_list = output_logits_list.view(num_decoder_layer, -1, num_vocab)
                gather_idx = cross_layer_sampled_ids_ts.unsqueeze(1).expand(-1, num_vocab).unsqueeze(0)
                gather_logits = output_logits_list.gather(0, gather_idx).view(batch_size, num_tokens, num_vocab)

                target_mask = tgt_tokens.ne(self.pad)
                # if self.args.use_ctc:
                ctc_loss = self.sequence_ctc_loss_with_logits(
                    logits=gather_logits,
                    logit_mask=prev_output_tokens_mask,
                    targets=tgt_tokens,
                    target_mask=target_mask,
                    blank_index=self.tgt_dict.blank_index,
                    label_smoothing=self.args.label_smoothing,
                    reduce=reduce
                )
                all_sample_ctc_loss += ctc_loss

            ret_val = {
                "ctc_loss": {"loss": all_sample_ctc_loss / len(output_logits_list)},
            }

        else:
            # if self.args.use_ctc:
            all_layer_ctc_loss = 0
            normalized_factor = 0
            for layer_idx, word_ins_out in enumerate(output_logits_list):
                ctc_loss = self.sequence_ctc_loss_with_logits(
                    logits=word_ins_out,
                    logit_mask=prev_output_tokens_mask,
                    targets=tgt_tokens,
                    target_mask=target_mask,
                    blank_index=self.tgt_dict.blank_index,
                    label_smoothing=self.args.label_smoothing, #NOTE: enable and double check with it later
                    reduce=reduce
                )
                factor = 1  # math.sqrt(layer_idx + 1)
                all_layer_ctc_loss += ctc_loss * factor
                normalized_factor += factor
            ret_val = {
                "ctc_loss": {"loss": all_layer_ctc_loss / normalized_factor},
            }


        softcopy_learnable = getattr(self.decoder, "softcopy_learnable", False)
        if softcopy_learnable:
            ret_val.update(
                {
                    "stat:softcopy_temp": self.decoder.para_softcopy_temp.item()
                }
            )

        return ret_val

    def forward_decoder(self, decoder_out, encoder_out, decoding_format=None, **kwargs):
        # set CTC decoder beam size
        self.ctc_decoder._beam_width = self.args.ctc_beam_size
        step = decoder_out.step
        output_tokens = decoder_out.output_tokens
        history = decoder_out.history

        # execute the decoder
        output_logits_list = self.decoder(
            normalize=False,
            prev_output_tokens=output_tokens,
            encoder_out=encoder_out,
            step=step,
        )

        inference_decoder_layer = self.inference_decoder_layer
        output_logits = output_logits_list[inference_decoder_layer]

        if self.plain_ctc:
            output_scores = decoder_out.output_scores
            _scores, _tokens = output_logits.max(-1)
            output_masks = output_tokens.ne(self.pad)
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
        else:
            # _scores, _tokens = F.log_softmax(output_logits, -1).max(-1)
            # _scores == beam_results[:,0,:]
            output_length = torch.sum(output_tokens.ne(self.tgt_dict.pad_index), dim=-1)
            beam_results, beam_scores, timesteps, out_lens = self.ctc_decoder.decode(F.softmax(output_logits, -1),
                                                                                     output_length)
            top_beam_tokens = beam_results[:, 0, :]
            top_beam_len = out_lens[:, 0]
            mask = torch.arange(0, top_beam_tokens.size(1)).type_as(top_beam_len). \
                repeat(top_beam_len.size(0), 1).lt(top_beam_len.unsqueeze(1))
            top_beam_tokens[~mask] = self.decoder.dictionary.pad()
            # output_scores.masked_scatter_(output_masks, _scores[output_masks])
            if history is not None:
                history.append(output_tokens.clone())

            return decoder_out._replace(
                output_tokens=top_beam_tokens.to(output_logits.device),
                output_scores=torch.full(top_beam_tokens.size(), 1.0),
                attn=None,
                history=history,
            )

    def get_search_results(self, decoder_out, encoder_out, beam_size=None, decoding_format=None, **kwargs):
        step = decoder_out.step
        output_tokens = decoder_out.output_tokens
        history = decoder_out.history
        # Set ctc beam size
        if beam_size is not None:
            self.ctc_decoder._beam_width = beam_size
        else:
            beam_size = self.ctc_decoder._beam_width

        # execute the decoder
        output_logits = self.decoder(
            normalize=False,
            prev_output_tokens=output_tokens,
            encoder_out=encoder_out,
            step=step,
        )
        # _scores, _tokens = F.log_softmax(output_logits, -1).max(-1)
        # _scores == beam_results[:,0,:]
        output_length = torch.sum(output_tokens.ne(self.tgt_dict.pad_index), dim=-1)
        beam_results, beam_scores, timesteps, out_lens = self.ctc_decoder.decode(F.softmax(output_logits, -1),
                                                                                 output_length)

        beam_results = beam_results[:, :, :out_lens.max()]
        beam_size = beam_scores.size(1)
        for beam_idx in range(beam_size):
            top_beam_tokens = beam_results[:, beam_idx, :]
            top_beam_len = out_lens[:, beam_idx]
            mask = torch.arange(0, top_beam_tokens.size(1)).type_as(top_beam_len). \
                repeat(top_beam_len.size(0), 1).lt(top_beam_len.unsqueeze(1))
            top_beam_tokens[~mask] = self.decoder.dictionary.pad()
        return beam_results, beam_scores

    def initialize_output_tokens_by_src_tokens(self, src_tokens):
        if not self.args.copy_src_token:
            length_tgt = torch.sum(src_tokens.ne(self.tgt_dict.pad_index), -1)
            if self.args.src_upsample_scale > 2:
                length_tgt = length_tgt * self.args.src_upsample_scale
            else:
                length_tgt = length_tgt * self.args.src_upsample_scale  # + 10
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
            return initial_output_tokens
        else:
            if self.args.src_upsample_scale <= 1:
                return src_tokens

            def _us(x, s):
                B = x.size(0)
                _x = x.unsqueeze(-1).expand(B, -1, s).reshape(B, -1)
                return _x

            return _us(src_tokens, self.args.src_upsample_scale)

    def initialize_output_tokens(self, encoder_out, src_tokens):
        # length prediction
        initial_output_tokens = self.initialize_output_tokens_by_src_tokens(src_tokens)

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


# class NATransformerDecoder(FairseqNATSharedDecoder):
#     def __init__(self, args, dictionary, embed_tokens, no_encoder_attn=False):
#         super().__init__(
#             args, dictionary, embed_tokens, no_encoder_attn=no_encoder_attn
#         )
#         self.dictionary = dictionary
#         self.bos = dictionary.bos()
#         self.unk = dictionary.unk()
#         self.eos = dictionary.eos()
#
#         self.encoder_embed_dim = args.encoder_embed_dim
#         self.sg_length_pred = getattr(args, "sg_length_pred", False)
#         self.pred_length_offset = getattr(args, "pred_length_offset", False)
#         self.length_loss_factor = getattr(args, "length_loss_factor", 0.1)
#         self.src_embedding_copy = getattr(args, "src_embedding_copy", False)
#         self.embed_length = Embedding(256, self.encoder_embed_dim, None)
#
#     @ensemble_decoder
#     def forward(self, normalize, encoder_out, prev_output_tokens, step=0, **unused):
#         features, all_features = self.extract_features(
#             prev_output_tokens,
#             encoder_out=encoder_out,
#             embedding_copy=(step == 0) & self.src_embedding_copy,
#         )
#         all_layer_output_logits = all_features['all_layer_output_logits']
#         return [F.log_softmax(x.transpose(0, 1), -1) if normalize else x.transpose(0, 1) for x in all_layer_output_logits]
#
#     @ensemble_decoder
#     def forward_length(self, normalize, encoder_out):
#         enc_feats = encoder_out["encoder_out"][0]  # T x B x C
#         if len(encoder_out["encoder_padding_mask"]) > 0:
#             src_masks = encoder_out["encoder_padding_mask"][0]  # B x T
#         else:
#             src_masks = None
#         enc_feats = _mean_pooling(enc_feats, src_masks)
#         if self.sg_length_pred:
#             enc_feats = enc_feats.detach()
#         length_out = F.linear(enc_feats, self.embed_length.weight)
#         return F.log_softmax(length_out, -1) if normalize else length_out
#
#     def extract_features(
#         self,
#         prev_output_tokens,
#         encoder_out=None,
#         early_exit=None,
#         embedding_copy=False,
#         **unused
#     ):
#         """
#         Similar to *forward* but only return features.
#
#         Inputs:
#             prev_output_tokens: Tensor(B, T)
#             encoder_out: a dictionary of hidden states and masks
#
#         Returns:
#             tuple:
#                 - the decoder's features of shape `(batch, tgt_len, embed_dim)`
#                 - a dictionary with any model-specific outputs
#             the LevenshteinTransformer decoder has full-attention to all generated tokens
#         """
#         # embedding
#         if embedding_copy:
#             src_embd = encoder_out["encoder_embedding"][0]
#             if len(encoder_out["encoder_padding_mask"]) > 0:
#                 src_mask = encoder_out["encoder_padding_mask"][0]
#             else:
#                 src_mask = None
#             src_mask = (
#                 ~src_mask
#                 if src_mask is not None
#                 else prev_output_tokens.new_ones(*src_embd.size()[:2]).bool()
#             )
#
#             x, decoder_padding_mask = self.forward_embedding(
#                 prev_output_tokens,
#                 self.forward_copying_source(
#                     src_embd, src_mask, prev_output_tokens.ne(self.padding_idx)
#                 ),
#             )
#
#         else:
#
#             x, decoder_padding_mask = self.forward_embedding(prev_output_tokens)
#
#         # B x T x C -> T x B x C
#         x = x.transpose(0, 1)
#         attn = None
#         inner_states = [x]
#         # layer_output_list = []
#         # layer_out = torch.zeros(x.size()).to(x)
#         # decoder layers
#         all_layer_output_logits = []
#         temperature = self.args.yhat_temp  # NOTE
#         for i, layer in enumerate(self.layers):
#             # early exit from the decoder.
#             if (early_exit is not None) and (i >= early_exit):
#                 break
#             layer_out_logits = self.output_layer(x)
#             if not self.args.hard_argmax:
#                 # soft argmax
#                 layer_out = torch.matmul(
#                     torch.softmax(layer_out_logits.detach() / temperature, dim=-1).view(-1, layer_out_logits.size(-1)),
#                     self.embed_tokens.weight).view(x.size())
#             else:
#                 # hard argmax
#                 layer_out = self.embed_tokens(layer_out_logits.argmax(dim=-1))
#             if i > 0:
#                 all_layer_output_logits.append(layer_out_logits)
#                 new_x = (x + layer_out) / torch.sqrt(torch.tensor(2.))
#             else:
#                 new_x = x
#
#             x, attn, _ = layer(
#                 new_x,
#                 encoder_out["encoder_out"][0]
#                 if (encoder_out is not None and len(encoder_out["encoder_out"]) > 0)
#                 else None,
#                 encoder_out["encoder_padding_mask"][0]
#                 if (
#                     encoder_out is not None
#                     and len(encoder_out["encoder_padding_mask"]) > 0
#                 )
#                 else None,
#                 self_attn_mask=None,
#                 self_attn_padding_mask=decoder_padding_mask,
#             )
#             inner_states.append(x)
#
#             # layer_out, _ = self.forward_embedding(layer_out_tokens)
#             # layer_output_list.append(layer_out_logits)
#
#         all_layer_output_logits.append(self.output_layer(x))
#         if self.layer_norm:
#             x = self.layer_norm(x)
#
#         # T x B x C -> B x T x C
#         x = x.transpose(0, 1)
#
#         # layer_output_list = [_x.transpose(0, 1) for _x in layer_output_list]
#
#         if self.project_out_dim is not None:
#             x = self.project_out_dim(x)
#
#         return x, {"attn": attn, "inner_states": inner_states, "all_layer_output_logits": all_layer_output_logits}
#
#     def forward_embedding(self, prev_output_tokens, states=None):
#         # embed positions
#         positions = (
#             self.embed_positions(prev_output_tokens)
#             if self.embed_positions is not None
#             else None
#         )
#
#         # embed tokens and positions
#         if states is None:
#             x = self.embed_scale * self.embed_tokens(prev_output_tokens)
#             if self.project_in_dim is not None:
#                 x = self.project_in_dim(x)
#         else:
#             x = states
#
#         if positions is not None:
#             x += positions
#         x = self.dropout_module(x)
#         decoder_padding_mask = prev_output_tokens.eq(self.padding_idx)
#         return x, decoder_padding_mask
#
#     def forward_copying_source(self, src_embeds, src_masks, tgt_masks):
#         length_sources = src_masks.sum(1)
#         length_targets = tgt_masks.sum(1)
#         mapped_inputs = _uniform_assignment(length_sources, length_targets).masked_fill(
#             ~tgt_masks, 0
#         )
#         copied_embedding = torch.gather(
#             src_embeds,
#             1,
#             mapped_inputs.unsqueeze(-1).expand(
#                 *mapped_inputs.size(), src_embeds.size(-1)
#             ),
#         )
#         return copied_embedding
#
#     def forward_length_prediction(self, length_out, encoder_out, tgt_tokens=None):
#         enc_feats = encoder_out["encoder_out"][0]  # T x B x C
#         if len(encoder_out["encoder_padding_mask"]) > 0:
#             src_masks = encoder_out["encoder_padding_mask"][0]  # B x T
#         else:
#             src_masks = None
#         if self.pred_length_offset:
#             if src_masks is None:
#                 src_lengs = enc_feats.new_ones(enc_feats.size(1)).fill_(
#                     enc_feats.size(0)
#                 )
#             else:
#                 src_lengs = (~src_masks).transpose(0, 1).type_as(enc_feats).sum(0)
#             src_lengs = src_lengs.long()
#
#         if tgt_tokens is not None:
#             # obtain the length target
#             tgt_lengs = tgt_tokens.ne(self.padding_idx).sum(1).long()
#             if self.pred_length_offset:
#                 length_tgt = tgt_lengs - src_lengs + 128
#             else:
#                 length_tgt = tgt_lengs
#             length_tgt = length_tgt.clamp(min=0, max=255)
#
#         else:
#             # predict the length target (greedy for now)
#             # TODO: implementing length-beam
#             pred_lengs = length_out.max(-1)[1]
#             if self.pred_length_offset:
#                 length_tgt = pred_lengs - 128 + src_lengs
#             else:
#                 length_tgt = pred_lengs
#
#         return length_tgt


@register_model_architecture(
    "nat_ctc_sd", "nat_ctc_sd"
)
def base_architecture(args):
    args.encoder_embed_path = getattr(args, "encoder_embed_path", None)
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_ffn_embed_dim = getattr(args, "encoder_ffn_embed_dim", 2048)
    args.encoder_layers = getattr(args, "encoder_layers", 6)
    args.encoder_attention_heads = getattr(args, "encoder_attention_heads", 8)
    args.encoder_normalize_before = getattr(args, "encoder_normalize_before", False)  # NOTE
    args.encoder_learned_pos = getattr(args, "encoder_learned_pos", False)
    args.decoder_embed_path = getattr(args, "decoder_embed_path", None)
    args.decoder_embed_dim = getattr(args, "decoder_embed_dim", args.encoder_embed_dim)
    args.decoder_ffn_embed_dim = getattr(
        args, "decoder_ffn_embed_dim", args.encoder_ffn_embed_dim
    )
    args.decoder_layers = getattr(args, "decoder_layers", 6)
    args.decoder_attention_heads = getattr(args, "decoder_attention_heads", 8)
    args.decoder_normalize_before = getattr(args, "decoder_normalize_before", False)  # NOTE
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
    "nat_ctc_sd", "nat_ctc_cross_layer_hidden_replace_deep_sup"
)
def base_architecture(args):
    args.encoder_embed_path = getattr(args, "encoder_embed_path", None)
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_ffn_embed_dim = getattr(args, "encoder_ffn_embed_dim", 2048)
    args.encoder_layers = getattr(args, "encoder_layers", 6)
    args.encoder_attention_heads = getattr(args, "encoder_attention_heads", 8)
    args.encoder_normalize_before = getattr(args, "encoder_normalize_before", False)  # NOTE
    args.encoder_learned_pos = getattr(args, "encoder_learned_pos", False)
    args.decoder_embed_path = getattr(args, "decoder_embed_path", None)
    args.decoder_embed_dim = getattr(args, "decoder_embed_dim", args.encoder_embed_dim)
    args.decoder_ffn_embed_dim = getattr(
        args, "decoder_ffn_embed_dim", args.encoder_ffn_embed_dim
    )
    args.decoder_layers = getattr(args, "decoder_layers", 6)
    args.decoder_attention_heads = getattr(args, "decoder_attention_heads", 8)
    args.decoder_normalize_before = getattr(args, "decoder_normalize_before", False)  # NOTE
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
    "nat_ctc_sd", "nat_ctc_sd_12d"
)
def base_architecture1(args):
    args.encoder_embed_path = getattr(args, "encoder_embed_path", None)
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_ffn_embed_dim = getattr(args, "encoder_ffn_embed_dim", 2048)
    args.encoder_layers = getattr(args, "encoder_layers", 6)
    args.encoder_attention_heads = getattr(args, "encoder_attention_heads", 8)
    args.encoder_normalize_before = getattr(args, "encoder_normalize_before", False)  # NOTE
    args.encoder_learned_pos = getattr(args, "encoder_learned_pos", False)
    args.decoder_embed_path = getattr(args, "decoder_embed_path", None)
    args.decoder_embed_dim = getattr(args, "decoder_embed_dim", args.encoder_embed_dim)
    args.decoder_ffn_embed_dim = getattr(
        args, "decoder_ffn_embed_dim", args.encoder_ffn_embed_dim
    )
    args.decoder_layers = getattr(args, "decoder_layers", 12)
    args.decoder_attention_heads = getattr(args, "decoder_attention_heads", 8)
    args.decoder_normalize_before = getattr(args, "decoder_normalize_before", False)  # NOTE
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
    "nat_ctc_sd", "nat_ctc_sd_de_24d"
)
def base_architecture2(args):
    args.encoder_embed_path = getattr(args, "encoder_embed_path", None)
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_ffn_embed_dim = getattr(args, "encoder_ffn_embed_dim", 2048)
    args.encoder_layers = getattr(args, "encoder_layers", 6)
    args.encoder_attention_heads = getattr(args, "encoder_attention_heads", 8)
    args.encoder_normalize_before = getattr(args, "encoder_normalize_before", False)  # NOTE
    args.encoder_learned_pos = getattr(args, "encoder_learned_pos", False)
    args.decoder_embed_path = getattr(args, "decoder_embed_path", None)
    args.decoder_embed_dim = getattr(args, "decoder_embed_dim", args.encoder_embed_dim)
    args.decoder_ffn_embed_dim = getattr(
        args, "decoder_ffn_embed_dim", args.encoder_ffn_embed_dim
    )
    args.decoder_layers = getattr(args, "decoder_layers", 24)
    args.decoder_attention_heads = getattr(args, "decoder_attention_heads", 8)
    args.decoder_normalize_before = getattr(args, "decoder_normalize_before", False)  # NOTE
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
