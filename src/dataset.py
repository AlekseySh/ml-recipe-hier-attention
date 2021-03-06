import random
import re
from functools import lru_cache
from pathlib import Path
from typing import Tuple, List, Dict, Union, Iterator

import numpy as np
import torch
from nltk.tokenize import PunktSentenceTokenizer, WordPunctTokenizer
from torch import LongTensor, FloatTensor
from torch.utils.data import Sampler, DataLoader
from tqdm.auto import tqdm

from src.const import IMBD_ROOT

TText = List[List[int]]  # text[i_sentece][j_word]
TItem = Dict[str, Union[TText, int]]

# params was choosen as 98% quantile:
SNT_CLIP = 100
TXT_CLIP = 40


class ImdbReviewsDataset:
    _path_to_data: Path
    _snt_clip: int
    _txt_clip: int
    _s_tokenizer: PunktSentenceTokenizer
    _w_tokenizer: WordPunctTokenizer

    # data fields
    _paths: List[Path]
    _texts: List[TText]
    _labels: List[int]
    _txt_lens: List[int]
    _snt_lens: List[int]
    _vocab: Dict[str, int]

    def __init__(self,
                 path_to_data: Path,
                 vocab: Dict[str, int],
                 snt_clip: int = SNT_CLIP,
                 txt_clip: int = TXT_CLIP
                 ):
        self._path_to_data = path_to_data
        self._vocab = vocab
        self._snt_clip = snt_clip
        self._txt_clip = txt_clip

        self._s_tokenizer = PunktSentenceTokenizer()
        self._w_tokenizer = WordPunctTokenizer()
        self._html_re = re.compile('<.*?>')

        self._paths = []
        self._texts = []
        self._labels = []
        self._txt_lens = []
        self._snt_lens = []

        self._load_data()

    def __len__(self) -> int:
        return len(self._texts)

    @lru_cache(maxsize=50_000)  # equal to number of reviews in imdb
    def __getitem__(self, i: int) -> TItem:
        return {
            'txt': self._texts[i],
            'label': self._labels[i],
            'txt_len': self._txt_lens[i],
            'snt_len': self._snt_lens[i]
        }

    def _load_data(self) -> None:
        files = list((self._path_to_data / 'neg').glob('*_*.txt')) + \
                list((self._path_to_data / 'pos').glob('*_*.txt'))

        print(f'Dataset loading from {self._path_to_data}.')
        for file_path in tqdm(files):
            with open(file_path, 'r') as f:
                text, snt_len_max, txt_len = self.tokenize_plane_text(f.read())
                label = 1 if file_path.parent.name == 'pos' else 0

                self._paths.append(file_path)
                self._texts.append(text)
                self._labels.append(label)
                self._snt_lens.append(snt_len_max)
                self._txt_lens.append(txt_len)

    def tokenize_plane_text(self, text_plane: str
                            ) -> Tuple[TText, int, int]:
        tokenize_w = self._w_tokenizer.tokenize
        tokenize_s = self._s_tokenizer.tokenize

        text_plane = text_plane.lower()
        text_plane = re.sub(self._html_re, ' ', text_plane)
        text = [[self.vocab[w] for w in tokenize_w(s)
                 if w in self._vocab.keys()][:self._snt_clip]
                for s in tokenize_s(text_plane)][:self._txt_clip]

        snt_len_max = max([len(snt) for snt in text])
        txt_len = len(text)

        return text, snt_len_max, txt_len

    @staticmethod
    def get_imdb_vocab(imdb_root: Path) -> Dict[str, int]:
        with open(imdb_root / 'imdb.vocab') as f:
            words = f.read().splitlines()

        # note, that we keep 0 for padding token
        ids = list(range(1, len(words) + 1))
        vocab = dict(zip(words, ids))

        return vocab

    @property
    def vocab(self) -> Dict[str, int]:
        return self._vocab

    @property
    def txt_lens(self) -> List[int]:
        return self._txt_lens


def collate_docs(batch: List[TItem]
                 ) -> Dict[str, Union[LongTensor, FloatTensor]]:
    max_snt = max([item['snt_len'] for item in batch])
    max_txt = max([item['txt_len'] for item in batch])

    n_docs = len(batch)  # number of documents in batch
    labels_tensor = torch.zeros((n_docs, 1), dtype=torch.float32)
    docs_tensor = torch.zeros((n_docs, max_txt, max_snt),
                              dtype=torch.int64)

    for i_doc, item in enumerate(batch):
        labels_tensor[i_doc] = item['label']

        for i_snt, snt in enumerate(item['txt']):  # type: ignore
            snt_len = len(snt)
            docs_tensor[i_doc, i_snt, 0:snt_len] = torch.tensor(snt)

    return {'features': docs_tensor, 'targets': labels_tensor}


class SimilarRandSampler(Sampler):
    _ids: List[int]
    _bs: int
    _k: int
    _len: int

    def __init__(self,
                 keys: List[int],
                 bs: int,
                 diversity: int = 10
                 ):
        super().__init__(data_source=None)

        assert (bs >= 1) & (diversity >= 1)

        self._ids = np.argsort(keys.copy()).tolist()
        self._bs = bs
        self._k = diversity

        self._len = int(np.ceil(len(self._ids) / self._bs))

    def __iter__(self) -> Iterator[int]:
        cur_ids = self._ids.copy()

        similar_key_batches = []
        while cur_ids:
            idx = random.choice(range(len(cur_ids)))
            lb = max(0, idx - self._k * self._bs)
            rb = min(len(cur_ids), idx + self._k * self._bs)

            batch = random.sample(cur_ids[lb: rb], min(self._bs, rb - lb))

            # rm ids from current batch from our pull
            cur_ids = [e for e in cur_ids if e not in batch]

            similar_key_batches.extend(batch)

        return iter(similar_key_batches)

    def __len__(self) -> int:
        return self._len


def get_datasets(imbd_root: Path = IMBD_ROOT
                 ) -> Tuple[ImdbReviewsDataset, ImdbReviewsDataset]:
    vocab = ImdbReviewsDataset.get_imdb_vocab(imbd_root)
    train_set = ImdbReviewsDataset(imbd_root / 'train', vocab)
    test_set = ImdbReviewsDataset(imbd_root / 'test', vocab)

    print(f'Train dataset was loaded, {len(train_set)} samples.\n'
          f'Test dataset was loaded, {len(test_set)} samples.')

    return train_set, test_set


def get_test_dataset(imbd_root: Path = IMBD_ROOT) -> ImdbReviewsDataset:
    vocab = ImdbReviewsDataset.get_imdb_vocab(imbd_root)
    return ImdbReviewsDataset(imbd_root / 'test', vocab)


def get_loaders(batch_size: int,
                n_workers: int = 4,
                imbd_root: Path = IMBD_ROOT,
                ) -> Tuple[DataLoader, DataLoader, Dict[str, int]]:
    train_set, test_set = get_datasets(imbd_root=imbd_root)

    args = {'num_workers': n_workers, 'batch_size': batch_size,
            'collate_fn': collate_docs}

    train_loader = DataLoader(
        dataset=train_set,
        sampler=SimilarRandSampler(keys=train_set.txt_lens,
                                   bs=batch_size),
        **args
    )

    test_loader = DataLoader(
        dataset=test_set,
        sampler=SimilarRandSampler(keys=test_set.txt_lens,
                                   bs=batch_size),
        **args
    )

    return train_loader, test_loader, train_loader.dataset.vocab
