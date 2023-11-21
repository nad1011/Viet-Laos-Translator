import logging
import math
import operator

import numpy as np
import torch
import torch.nn.functional as functional
from torch.autograd import Variable
from torch.nn.utils.rnn import pad_sequence

from module.inference.decode_strategy import DecodeStrategy
from utils.misc import no_peeking_mask


class BeamSearch(DecodeStrategy):
	def __init__(self, model, max_len, device,
	             beam_size=5, use_synonym_fn=False, replace_unk=None, length_normalize=None):
		"""
		Args:
			model: the used model
			max_len: the maximum timestep to be used
			device: the device to perform calculation
			beam_size: the size of the beam itself
			use_synonym_fn: if set, use the get_synonym fn from wordnet to try replace <unk>
			replace_unk: a tuple of [layer, head] designation, to replace the unknown word by chosen attention
		"""
		super(BeamSearch, self).__init__(model, max_len, device)
		self.beam_size = beam_size
		self._use_synonym = use_synonym_fn
		self._replace_unk = replace_unk
		self._length_norm = length_normalize

	def init_vars(self, src):
		"""
		Calculate the required matrices during translation after the model is finished
		Input:
		:param src: The batch of sentences

		Output: Initialize the first character includes outputs, e_outputs, log_scores
		"""
		model = self.model
		batch_size = len(src)
		row_b = self.beam_size * batch_size

		init_tok = self.TRG.vocab.stoi['<sos>']
		src_mask = (src != self.SRC.vocab.stoi['<pad>']).unsqueeze(-2).to(self.device)
		src = src.to(self.device)

		# Encoder
		# raise Exception(src.shape, src_mask.shape)
		e_output = model.encode(src, src_mask)
		outputs = torch.LongTensor([[init_tok] for _ in range(batch_size)])
		outputs = outputs.to(self.device)
		trg_mask = no_peeking_mask(1, self.device)

		# Decoder
		out = model.to_logits(model.decode(outputs, e_output, src_mask, trg_mask))
		out = functional.softmax(out, dim=-1)
		probs, ix = out[:, -1].data.topk(self.beam_size)

		log_scores = torch.Tensor([math.log(p) for p in probs.data.view(-1)]).view(-1, 1)

		outputs = torch.zeros(row_b, self.max_len).long()
		outputs = outputs.to(self.device)
		outputs[:, 0] = init_tok
		outputs[:, 1] = ix.view(-1)

		e_outputs = torch.repeat_interleave(e_output, self.beam_size, 0)

		# raise Exception(outputs[:, :2], e_outputs)

		return outputs, e_outputs, log_scores

	def compute_k_best(self, outputs, out, log_scores, i):
		"""
		Compute k words with the highest conditional probability
		Args:
			outputs: Array has k previous candidate output sequences. [batch_size*beam_size, max_len]
			i: the current timestep to execute. Int
			out: current output of the model at timestep. [batch_size*beam_size, vocab_size]
			log_scores: Conditional probability of past candidates (in outputs) [batch_size * beam_size]

		Returns:
			new outputs has k best candidate output sequences
			log_scores for each of those candidate
		"""
		row_b = len(out)
		batch_size = row_b // self.beam_size
		eos_id = self.TRG.vocab.stoi['<eos>']

		probs, ix = out[:, -1].data.topk(self.beam_size)

		probs_rep = torch.Tensor([[1] + [1e-100] * (self.beam_size - 1)] * row_b).view(row_b, self.beam_size).to(
			self.device)
		ix_rep = torch.LongTensor([[eos_id] + [-1] * (self.beam_size - 1)] * row_b).view(row_b, self.beam_size).to(
			self.device)

		check_eos = torch.repeat_interleave((outputs[:, i - 1] == eos_id).view(row_b, 1), self.beam_size, 1)

		probs = torch.where(check_eos, probs_rep, probs)
		ix = torch.where(check_eos, ix_rep, ix)

		#        if(debug):
		#            print("kprobs before debug: ", probs, probs_rep, ix, ix_rep, log_scores)

		log_probs = torch.log(probs).to(self.device) + log_scores.to(self.device)  # CPU

		k_probs, k_ix = log_probs.view(batch_size, -1).topk(self.beam_size)

		# Use cpu
		k_probs, k_ix = torch.Tensor(k_probs.cpu().data.numpy()), torch.LongTensor(k_ix.cpu().data.numpy())
		row = k_ix // self.beam_size + torch.LongTensor([[v * self.beam_size] for v in range(batch_size)])
		col = k_ix % self.beam_size

		outputs[:, :i] = outputs[row.view(-1), :i]
		outputs[:, i] = ix[row.view(-1), col.view(-1)]
		log_scores = k_probs.view(-1, 1)

		return outputs, log_scores

	def replace_unknown(self, outputs, sentences, attn, selector_tuple, unknown_token="<unk>"):
		"""Replace the unknown words in the outputs with the highest valued attentionized words.
		Args:
			outputs: the output from decoding. [batch, beam] of list of str
			sentences: the original wordings of the sentences. [batch_size, src_len] of str
			attn: the attention received, in the form of list:  [layers units of (self-attention, attention) with shapes of [batchbeam, heads, tgt_len, tgt_len] & [batchbeam, heads, tgt_len, src_len] respectively]
			selector_tuple: (layer, head) used to select the attention
			unknown_token: token used for checking. str
		Returns:
			the replaced version, in the same shape as outputs
			"""

		layer_used, head_used = selector_tuple
		# it should be [batchbeam, tgt_len, src_len], as we are using the attention to source
		used_attention = attn[layer_used][-1][:, head_used]
		# flatten the outputs back to batchbeam
		flattened_outputs = outputs.reshape((-1,))
		# [batchbeam, tgt_len] of the best indices
		# Also convert to numpy version (remove sos not needed as it is attention of outputs)
		select_id_src = torch.argmax(used_attention, dim=-1).cpu().numpy()
		# used custom-calculated beam_size as we might not output the entirety of beams.
		# See beam_search fn for details
		beam_size = select_id_src.shape[0] // len(sentences)
		# select per batchbeam. source batch id is found by dividing batchbeam id per beam;
		# we are selecting [tgt_len] indices from [src_len] tokens;
		# then concat at the first dimensions to retrieve [batch_beam, tgt_len] of replacement tokens
		# need itemgetter / map to retrieve from list
		replace_tokens = [operator.itemgetter(*src_idx)(sentences[bidx // beam_size])
		                  for bidx, src_idx in enumerate(select_id_src)]

		# zip together with sentences; then output { the token if not unk / the replacement if is }
		# Note that this will trim the orig version down to repl size.
		zipped = zip(flattened_outputs, replace_tokens)
		replaced = np.array(
			[[tok if tok != unknown_token else rpl for rpl, tok in zip(repl, orig)] for orig, repl in zipped])
		# reshape back to outputs shape [batch, beam] of list
		return replaced.reshape(outputs.shape)

	def beam_search(self, batch, src_tokens=None, n_best=1, length_norm=None, replace_unk=None, debug=False):
		"""
		Beam search select k words with the highest conditional probability to be the first word
		of the k candidate output sequences.
		Args:
			batch: The batch of sentences, already in [batch_size, tokens] of int
			src_tokens: src in str version, same size as above.
			Used almost exclusively for replace unknown word
			n_best: number of usable values per beam loaded
			length_norm: if specified, normalize as per (Wu, 2016);
			note that if not inputted then it will still use __init__ value as default. float
			replace_unk: if specified, do replace unknown word using attention of (layer, head);
			note that if not inputted, it will still use __init__ value as default. (int, int)
			debug: if true, print some debug information during the search
		Return:
			An array of translated sentences, in list-of-tokens format.
			Either [batch_size, n_best, tgt_len] when n_best > 1
			Or [batch_size, tgt_len] when n_best == 1
		"""
		model = self.model
		outputs, e_outputs, log_scores = self.init_vars(batch)

		eos_tok = self.TRG.vocab.stoi['<eos>']
		src_mask = (batch != self.SRC.vocab.stoi['<pad>']).unsqueeze(-2)
		src_mask = torch.repeat_interleave(src_mask, self.beam_size, 0).to(self.device)
		is_finished = torch.LongTensor([[eos_tok] for i in range(self.beam_size * len(batch))]).view(-1).to(self.device)

		for i in range(2, self.max_len):
			trg_mask = no_peeking_mask(i, self.device)

			decoder_output, attn = model.decoder(outputs[:, :i], e_outputs, src_mask, trg_mask)
			out = model.out(decoder_output)
			out = functional.softmax(out, dim=-1)
			outputs, log_scores = self.compute_k_best(outputs, out, log_scores, i)

			# Occurrences of end symbols for all input sentences.
			if torch.equal(outputs[:, i], is_finished):
				break

		#        if(self._replace_unk):
		#            outputs = self.replace_unknown(attn, src, outputs)

		# reshape outputs and log_probs to [batch, beam] numpy array
		batch_size = batch.shape[0]
		outputs = outputs.cpu().numpy().reshape((batch_size, self.beam_size, self.max_len))
		log_scores = log_scores.cpu().numpy().reshape((batch_size, self.beam_size))

		# Get the best sentences for every beam: splice by length and itos the indices, result in an array of tokens
		# also remove the first token in this timestep (as it is sos)
		trim_and_itos = lambda sent: [self.TRG.vocab.itos[i] for i in sent[1:self._length(sent, eos_tok=eos_tok)]]
		translated_sentences = np.empty(outputs.shape[:-1], dtype=object)
		for ba in range(outputs.shape[0]):
			for bm in range(outputs.shape[1]):
				translated_sentences[ba, bm] = trim_and_itos(outputs[ba, bm])

		if replace_unk is None:
			replace_unk = self._replace_unk
		if replace_unk:
			# replace unknown words per translated sentences.
			# Do it before normalization (since that is independent on actual tokens)
			if src_tokens is None:
				logging.warn(
					"replace_unknown option enabled but no src_tokens supplied for the task. The method will not run.")
			else:
				translated_sentences = self.replace_unknown(translated_sentences, src_tokens, attn, replace_unk)

		if length_norm is None:
			length_norm = self._length_norm
		if length_norm is not None:
			# perform length normalization calculation and reorganize the sentences accordingly
			lengths = np.apply_along_axis(lambda x: self._length(x, eos_tok=eos_tok), -1, outputs)
			log_scores, indices = self.length_normalize(lengths, log_scores, coff=length_norm)
			translated_sentences = np.array([beams[ids] for beams, ids in zip(translated_sentences, indices)])

		return translated_sentences[:, 0] if n_best == 1 else translated_sentences[:, :n_best]

	def transl_batch(self, batch: list[str] | torch.Tensor, field_processed=False,
	                 src_size_limit=None, output_tokens=False, replace_unk=None):
		"""Translate a batch of sentences together. Currently disabling the synonym func.
		Args:
			batch: the batch of sentences to be translated
			field_processed: if the sentences had been already processed (i.e. part of batched validation data)
			src_size_limit: if set, trim the input if it crosses this value.
			Added due to current positional encoding support only <=200 tokens
			output_tokens: the output format.
			False will give a batch of sentences (str), while True will give batch of tokens (list of str)
			replace_unk: see beam_search for usage. (int, int) or False to suppress __init__ value
		Return:
			the result of translation, with format dictated by output_tokens
		"""
		self.model.eval()
		# create the indiced batch.
		processed_batch = self.preprocess_batch(batch, field_processed, src_size_limit, True)
		sent_ids, sent_tokens = (processed_batch, None) if field_processed else processed_batch

		assert isinstance(sent_ids, torch.Tensor), f'sent_ids is instead {type(sent_ids)}'

		translated_sentences = self.beam_search(sent_ids, src_tokens=sent_tokens, replace_unk=replace_unk)

		if not output_tokens:
			translated_sentences = [' '.join(tokens) for tokens in translated_sentences]

		return translated_sentences

	def preprocess_batch(self, batch: list[str] | torch.Tensor, field_processed=False,
	                     src_size_limit=None, output_tokens=False, pad_token="<pad>"):
		"""
		Adding
			src_size_limit: int, option to limit the length of src.
			src_lang: if specified (not None), append this token <{src_lang}> to the start of the batch
			field_processed: bool: if the sentences had been already processed (i.e. part of batched validation data)
			output_tokens: if set, output a token version aside the id version, in [batch of [src_len]] str.
			Note that it won't work with field_processed
		"""
		if field_processed:
			# do nothing, as it had already performed tokenizing/stoi.
			# Still cap the length of the batch due to possible infraction in valid
			return batch if src_size_limit is None else batch[:, :src_size_limit]

		processed_sent = map(self.SRC.preprocess, batch)

		if src_size_limit:
			processed_sent = map(lambda x: x[:src_size_limit], processed_sent)

		processed_sent = list(processed_sent)
		# convert to tensors, in indices format
		tokenized_sent = [torch.LongTensor([self._token_to_index(t) for t in s]) for s in processed_sent]
		# padding sentences
		batch = Variable(pad_sequence(tokenized_sent, True, padding_value=self.SRC.vocab.stoi[pad_token]))

		return batch, processed_sent if output_tokens else batch

	def length_normalize(self, lengths, log_probs, coff=0.6):
		"""Normalize the probabilty score as in (Wu 2016). Use pure numpy values
		Args:
			lengths: the length of the hypothesis. [batch, beam] of int->float
			log_probs: the unchanged log probability for the whole hypothesis. [batch, beam] of float
			coff: the alpha coefficient.
		Returns:
			Tuple of (penalized_values, indices) to reorganize outputs."""
		lengths = ((lengths + 5) / 6) ** coff
		penalized_probs = log_probs / lengths
		indices = np.argsort(penalized_probs, axis=-1)[::-1]
		# basically take log_probs values for every batch
		reorganized_probs = np.array([prb[ids] for prb, ids in zip(penalized_probs, indices)])
		return reorganized_probs, indices

	def _length(self, tokens, eos_tok=None):
		"""Retrieve the first location of eos_tok as length; else return the entire length"""
		if eos_tok is None:
			eos_tok = self.TRG.vocab.stoi['<eos>']
		eos, = np.nonzero(tokens == eos_tok)
		return len(tokens) if not eos else eos[0]

	def _token_to_index(self, tok):
		"""Override to select, depending on the self._use_synonym param"""
		return super(BeamSearch, self)._token_to_index(tok) if self._use_synonym else self.SRC.vocab.stoi[tok]
