'''Script containing the main dataset class'''

from pathlib import Path
from typing import Union, Dict, Tuple, List
import pandas as pd
import logging
import requests
import zipfile
import io
import shutil
from collections import defaultdict
import re
import multiprocessing as mp
from tqdm.auto import tqdm

from .twitter import Twitter
from .article import process_article_url
from .image import process_image_url


logging.getLogger('jieba').setLevel(logging.CRITICAL)
logger = logging.getLogger(__name__)


class MuminDataset:
    '''The MuMiN misinformation dataset, from [1].

    Args:
        twitter_bearer_token (str):
            The Twitter bearer token.
        size (str, optional):
            The size of the dataset. Can be either 'small', 'medium' or
            'large'. Defaults to 'large'.
        include_articles (bool, optional):
            Whether to include articles in the dataset. This will mean that
            compilation of the dataset will take a bit longer, as these need to
            be downloaded and parsed. Defaults to True.
        include_images (bool, optional):
            Whether to include images in the dataset. This will mean that
            compilation of the dataset will take a bit longer, as these need to
            be downloaded and parsed. Defaults to True.
        include_hashtags (bool, optional):
            Whether to include hashtags in the dataset. Defaults to True.
        include_mentions (bool, optional):
            Whether to include mentions in the dataset. Defaults to True.
        include_places (bool, optional):
            Whether to include places in the dataset. Defaults to True.
        include_polls (bool, optional):
            Whether to include polls in the dataset. Defaults to True.
        include_text_embeddings (bool, optional):
            Whether to compute embeddings for all texts in the dataset.
            Defaults to True.
        include_image_embeddings (bool, optional):
            Whether to compute embeddings for all images in the dataset.
            Defaults to True.
        text_embedding_model_id (str, optional):
            The HuggingFace Hub model ID to use when embedding texts. Defaults
            to 'sentence-transformers/paraphrase-multilingual-mpnet-base-v2'.
        image_embedding_model_id (str, optional):
            The HuggingFace Hub model ID to use when embedding images. Defaults
            to 'facebook/deit-base-distilled-patch16-224'.
        dataset_dir (str or pathlib Path, optional):
            The path to the folder where the dataset should be stored. Defaults
            to './mumin'.

    Attributes:
        twitter (Twitter object): A wrapper for the Twitter API.
        include_articles (bool): Whether to include articles in the dataset.
        include_images (bool): Whether to include images in the dataset.
        include_hashtags (bool): Whether to include hashtags in the dataset.
        include_mentions (bool): Whether to include mentions in the dataset.
        include_places (bool): Whether to include places in the dataset.
        include_polls (bool): Whether to include polls in the dataset.
        size (str): The size of the dataset.
        dataset_dir (pathlib Path): The dataset directory.
        nodes (dict): The nodes of the dataset.
        rels (dict): The relations of the dataset.

    References:
        - [1] Nielsen and McConville: _MuMiN: A Large-Scale Multilingual
              Multimodal Fact-Checked Misinformation Dataset with Linked Social
              Network Posts_ (2021)
    '''

    download_url: str = ('https://github.com/CLARITI-REPHRAIN/mumin-build/'
                         'raw/main/data/mumin.zip')
    _node_dump: List[str] = ['claim', 'tweet', 'user', 'image', 'article',
                             'place', 'hashtag', 'poll']
    _rel_dump: List[Tuple[str, str, str]] = [
        ('tweet', 'discusses', 'claim'),
        ('tweet', 'mentions', 'user'),
        ('tweet', 'located_in', 'place'),
        ('tweet', 'has_image', 'image'),
        ('tweet', 'has_hashtag', 'hashtag'),
        ('tweet', 'has_article', 'article'),
        ('tweet', 'has_poll', 'poll'),
        ('user', 'posted', 'tweet'),
        ('user', 'mentions', 'user'),
        ('user', 'has_pinned', 'tweet'),
        ('user', 'has_hashtag', 'hashtag'),
        ('user', 'has_profile_picture', 'image'),
        ('article', 'has_top_image', 'image'),
    ]

    def __init__(self,
                 twitter_bearer_token: str,
                 size: str = 'large',
                 include_articles: bool = True,
                 include_images: bool = True,
                 include_hashtags: bool = True,
                 include_mentions: bool = True,
                 include_places: bool = True,
                 include_polls: bool = True,
                 include_text_embeddings: bool = True,
                 include_image_embeddings: bool = True,
                 text_embedding_model_id: str = ('sentence-transformers/'
                                                 'paraphrase-multilingual-'
                                                 'mpnet-base-v2'),
                 image_embedding_model_id: str = ('facebook/deit-base-'
                                                  'distilled-patch16-224'),
                 dataset_dir: Union[str, Path] = './mumin'):
        self.twitter = Twitter(twitter_bearer_token=twitter_bearer_token)
        self.size = size
        self.include_articles = include_articles
        self.include_images = include_images
        self.include_hashtags = include_hashtags
        self.include_mentions = include_mentions
        self.include_places = include_places
        self.include_polls = include_polls
        self.include_text_embeddings = include_text_embeddings
        self.include_image_embeddings = include_image_embeddings
        self.text_embedding_model_id = text_embedding_model_id
        self.image_embedding_model_id = image_embedding_model_id
        self.dataset_dir = Path(dataset_dir)
        self.nodes: Dict[str, pd.DataFrame] = dict()
        self.rels: Dict[Tuple[str, str, str], pd.DataFrame] = dict()

    def __repr__(self) -> str:
        '''A string representation of the dataaset.

        Returns:
            str: The representation of the dataset.
        '''
        if len(self.nodes) == 0 or len(self.rels) == 0:
            return f'MuminDataset(size={self.size}, compiled=False)'
        else:
            num_nodes = sum([len(df) for df in self.nodes.values()])
            num_rels = sum([len(df) for df in self.rels.values()])
            return (f'MuminDataset(num_nodes={num_nodes:,}, '
                    f'num_relations={num_rels:,}, '
                    f'size=\'{self.size}\', '
                    f'compiled=False)')

    def compile(self, overwrite: bool = False):
        '''Compiles the dataset.

        This entails downloading the dataset, rehydrating the Twitter data and
        downloading the relevant associated data, such as articles and images.

        Args:
            overwrite (bool, optional):
                Whether the dataset directory should be overwritten, in case it
                already exists. Defaults to False.
        '''
        self._download(overwrite=overwrite)
        self._load_dataset()
        self._shrink_dataset()
        self._rehydrate()
        self._extract_nodes()
        self._extract_relations()
        self._extract_articles()
        self._extract_images()
        self._filter_node_features()
        self._remove_auxilliaries()
        self._dump_to_csv()

    def _download(self, overwrite: bool = False):
        '''Downloads and unzips the dataset.

        Args:
            overwrite (bool, optional):
                Whether the dataset directory should be overwritten, in case it
                already exists. Defaults to False.
        '''
        if (not self.dataset_dir.exists() or
                (self.dataset_dir.exists() and overwrite)):

            # Remove existing directory if we are overwriting
            if self.dataset_dir.exists() and overwrite:
                shutil.rmtree(self.dataset_dir)

            response = requests.get(self.download_url)

            # If the response was unsuccessful then raise an error
            if response.status_code != 200:
                msg = f'[{response.status_code}] {response.content}'
                raise RuntimeError(msg)

            # Otherwise unzip the in-memory zip file to `self.dataset_dir`
            else:
                zipped = response.content
                with zipfile.ZipFile(io.BytesIO(zipped)) as zip_file:
                    zip_file.extractall(self.dataset_dir)

    def _load_dataset(self):
        '''Loads the dataset files into memory.

        Raises:
            RuntimeError:
                If the dataset has not been downloaded yet.
        '''

        # Raise error if the dataset has not been downloaded yet
        if not self.dataset_dir.exists():
            raise RuntimeError('Dataset has not been downloaded yet!')

        # Loop over the files in the dataset directory
        csv_paths = [path for path in self.dataset_dir.iterdir()
                     if str(path)[-4:] == '.csv']
        for path in csv_paths:
            fname = path.stem

            # Node case: no underscores in file name
            if len(fname.split('_')) == 1:
                self.nodes[fname] = pd.read_csv(path)

            # Relation case: exactly two underscores in file name
            elif len(fname.split('_')) > 2:
                splits = fname.split('_')
                src = splits[0]
                tgt = splits[-1]
                rel = '_'.join(splits[1:-1])
                self.rels[(src, rel, tgt)] = pd.read_csv(path)

            # Otherwise raise error
            else:
                raise RuntimeError(f'Could not recognise {fname} as a node '
                                   f'or relation.')

        # Ensure that claims are present in the dataset
        if 'claim' not in self.nodes.keys():
            raise RuntimeError('No claims are present in the zipfile!')

        # Ensure that tweets are present in the dataset, and also that the
        # tweet IDs are unique
        if 'tweet' not in self.nodes.keys():
            raise RuntimeError('No tweets are present in the zipfile!')
        else:
            tweet_df = self.nodes['tweet']
            duplicated = (tweet_df[tweet_df.tweet_id.duplicated()].tweet_id
                                                                  .tolist())
            if len(duplicated) > 0:
                raise RuntimeError(f'The tweet IDs {duplicated} are '
                                   f'duplicate in the dataset!')

    def _shrink_dataset(self):
        '''Shrink dataset if `size` is 'small' or 'medium'''
        if self.size == 'small' or self.size == 'medium':

            # Define the `relevance` threshold
            if self.size == 'small':
                threshold = 0.8
            else:
                threshold = 0.75

            # Filter (:Tweet)-[:DISCUSSES]->(:Claim)
            discusses_rel = (self.rels[('tweet', 'discusses', 'claim')]
                             .query(f'relevance > {threshold}'))
            self.rels[('tweet', 'discusses', 'claim')] = discusses_rel

            # Filter tweets
            tweet_df = self.nodes['tweet']
            include_tweet = tweet_df.tweet_id.isin(discusses_rel.src.tolist())
            self.nodes['tweet'] = tweet_df[include_tweet]

            # Filter claims
            claim_df = self.nodes['claim']
            include_claim = claim_df.id.isin(discusses_rel.tgt.tolist())
            self.nodes['claim'] = claim_df[include_claim]

            # Filter (:Tweet)-[:DISCUSSES]->(:Claim)
            discusses_rel = (self.rels[('article', 'discusses', 'claim')]
                             .query(f'relevance > {threshold}'))
            self.rels[('article', 'discusses', 'claim')] = discusses_rel

            # Filter articles
            article_df = self.nodes['article']
            include_article = article_df.id.isin(discusses_rel.src.tolist())
            self.nodes['article'] = article_df[include_article]

            # Filter (:User)-[:POSTED]->(:Tweet)
            posted_rel = self.rels[('user', 'posted', 'tweet')]
            posted_rel = posted_rel[posted_rel.tgt.isin(self.nodes['tweet']
                                                            .tweet_id
                                                            .tolist())]
            self.rels[('user', 'posted', 'tweet')] = posted_rel

            # Filter (:Tweet)-[:MENTIONS]->(:User)
            mentions_rel = self.rels[('tweet', 'mentions', 'user')]
            mentions_rel = mentions_rel[mentions_rel
                                        .src
                                        .isin(self.nodes['tweet']
                                                  .tweet_id
                                                  .tolist())]
            self.rels[('tweet', 'mentions', 'user')] = mentions_rel

            # Filter users
            user_df = self.nodes['user']
            has_posted = user_df.user_id.isin(posted_rel.src.tolist())
            was_mentioned = user_df.user_id.isin(mentions_rel.tgt.tolist())
            self.nodes['user'] = user_df[has_posted | was_mentioned]

            # Filter (:User)-[:MENTIONS]->(:User)
            mentions_rel = self.rels[('user', 'mentions', 'user')]
            mentions_rel = mentions_rel[mentions_rel
                                        .src
                                        .isin(self.nodes['user']
                                                  .user_id
                                                  .tolist())]
            mentions_rel = mentions_rel[mentions_rel
                                        .tgt
                                        .isin(self.nodes['user']
                                                  .user_id
                                                  .tolist())]
            self.rels[('user', 'mentions', 'user')] = mentions_rel

    def _rehydrate(self):
        '''Rehydrate the tweets and users in the dataset'''

        # Ensure that the tweet and user IDs have been loaded into memory
        if 'tweet' not in self.nodes.keys():
            raise RuntimeError('Tweet IDs have not been loaded yet! '
                               'Load the dataset first.')

        # Only rehydrate if we have not rehydrated already; a simple way to
        # check this is to see if the tweet dataframe has the 'text'
        # column
        elif 'text' not in self.nodes['tweet'].columns:
            # Get the tweet IDs
            tweet_ids = self.nodes['tweet'].tweet_id.tolist()

            # Rehydrate the tweets
            tweet_dfs = self.twitter.rehydrate_tweets(tweet_ids=tweet_ids)

            # Extract and store tweets and users
            self.nodes['tweet'] = tweet_dfs['tweets']
            self.nodes['user'] = tweet_dfs['users']

            # Extract and store images
            if self.include_images and len(tweet_dfs['media']):
                video_query = '(type == "video") or (type == "animated gif")'
                video_df = (tweet_dfs['media']
                            .query(video_query)
                            .drop(columns=['url', 'duration_ms',
                                           'public_metrics.view_count'])
                            .rename(columns=dict(preview_image_url='url')))
                image_df = (tweet_dfs['media']
                            .query('type == "photo"')
                            .append(video_df))
                self.nodes['image'] = image_df

            # Extract and store polls
            if self.include_polls and len(tweet_dfs['polls']):
                self.nodes['poll'] = tweet_dfs['polls']

            # Extract and store places
            if self.include_places and len(tweet_dfs['places']):
                self.nodes['place'] = tweet_dfs['places']

            # TODO: Rehydrate quote tweets and replies

    def _extract_nodes(self):
        '''Extracts nodes from the raw Twitter data'''

        # Hashtags
        if self.include_hashtags:
            def extract_hashtag(dcts: List[dict]) -> List[str]:
                return [dct.get('tag') for dct in dcts]

            # Add hashtags from tweets
            if 'entities.hashtags' in self.nodes['tweet'].columns:
                hashtags = (self.nodes['tweet']['entities.hashtags']
                                .dropna()
                                .map(extract_hashtag)
                                .explode()
                                .tolist())
                node_df = pd.DataFrame(dict(tag=hashtags))
                if 'hashtag' in self.nodes.keys():
                    node_df = (self.nodes['hashtag'].append(node_df)
                                                    .drop_duplicates()
                                                    .reset_index(drop=True))
                self.nodes['hashtag'] = node_df

            # Add hashtags from users
            if 'entities.description.hashtags' in self.nodes['user'].columns:
                hashtags = (self.nodes['user']['entities.description.hashtags']
                                .dropna()
                                .map(extract_hashtag)
                                .explode()
                                .tolist())
                node_df  = pd.DataFrame(dict(tag=hashtags))
                if 'hashtag' in self.nodes.keys():
                    node_df = (self.nodes['hashtag'].append(node_df)
                                                    .drop_duplicates()
                                                    .reset_index(drop=True))
                self.nodes['hashtag'] = node_df

        # Add urls from tweets
        if 'entities.urls' in self.nodes['tweet'].columns:
            def extract_url(dcts: List[dict]) -> List[Union[str, None]]:
                return [dct.get('expanded_url') or dct.get('url')
                        for dct in dcts]
            urls = (self.nodes['tweet']['entities.urls']
                        .dropna()
                        .map(extract_url)
                        .explode()
                        .tolist())
            node_df = pd.DataFrame(dict(url=urls))
            if 'url' in self.nodes.keys():
                node_df = (self.nodes['url'].append(node_df)
                                            .drop_duplicates()
                                            .reset_index(drop=True))
            self.nodes['url'] = node_df

        # Add urls from user urls
        if 'entities.url.urls' in self.nodes['user'].columns:
            def extract_url(dcts: List[dict]) -> List[Union[str, None]]:
                return [dct.get('expanded_url') or dct.get('url')
                        for dct in dcts]
            urls = (self.nodes['user']['entities.url.urls']
                        .dropna()
                        .map(extract_url)
                        .explode()
                        .tolist())
            node_df = pd.DataFrame(dict(url=urls))
            if 'url' in self.nodes.keys():
                node_df = (self.nodes['url'].append(node_df)
                                            .drop_duplicates()
                                            .reset_index(drop=True))
            self.nodes['url'] = node_df

        # Add urls from user descriptions
        if 'entities.description.urls' in self.nodes['user'].columns:
            def extract_url(dcts: List[dict]) -> List[Union[str, None]]:
                return [dct.get('expanded_url') or dct.get('url')
                        for dct in dcts]
            urls = (self.nodes['user']['entities.description.urls']
                        .dropna()
                        .map(extract_url)
                        .explode()
                        .tolist())
            node_df = pd.DataFrame(dict(url=urls))
            if 'url' in self.nodes.keys():
                node_df = (self.nodes['url'].append(node_df)
                                            .drop_duplicates()
                                            .reset_index(drop=True))
            self.nodes['url'] = node_df

        # Add urls from profile pictures
        if (self.include_images and
                'profile_image_url' in self.nodes['user'].columns):
            def extract_url(dcts: List[dict]) -> List[Union[str, None]]:
                return [dct.get('expanded_url') or dct.get('url')
                        for dct in dcts]
            urls = (self.nodes['user']['profile_image_url']
                        .dropna()
                        .tolist())
            node_df = pd.DataFrame(dict(url=urls))
            if 'url' in self.nodes.keys():
                node_df = (self.nodes['url'].append(node_df)
                                            .drop_duplicates()
                                            .reset_index(drop=True))
            self.nodes['url'] = node_df

                                            .reset_index(drop=True))
            self.nodes['url'] = node_df

        # Add place features
        if self.include_places and 'place' in self.nodes.keys():
            def get_lat(bbox: list) -> float:
                return (bbox[1] + bbox[3]) / 2
            def get_lng(bbox: list) -> float:
                return (bbox[0] + bbox[2]) / 2
            place_df = self.nodes['place']
            place_df['lat'] = place_df['geo.bbox'].map(get_lat)
            place_df['lng'] = place_df['geo.bbox'].map(get_lng)
            self.nodes['place'] = place_df

        # Add poll features
        if self.include_polls and 'poll' in self.nodes.keys():
            def get_labels(options: List[dict]) -> List[str]:
                return [dct['label'] for dct in options]
            def get_votes(options: List[dict]) -> List[int]:
                return [dct['votes'] for dct in options]
            poll_df = self.nodes['poll']
            poll_df['labels'] = poll_df.options.map(get_labels)
            poll_df['votes'] = poll_df.options.map(get_votes)
            self.nodes['poll'] = poll_df

    def _extract_relations(self):
        '''Extracts relations from the raw Twitter data'''

        # (:User)-[:POSTED]->(:Tweet)
        merged = (self.nodes['tweet'][['author_id']]
                      .dropna()
                      .reset_index()
                      .rename(columns=dict(index='tweet_idx'))
                      .merge(self.nodes['user'][['user_id']]
                                 .reset_index()
                                 .rename(columns=dict(index='user_idx')),
                             left_on='author_id',
                             right_on='user_id'))
        data_dict = dict(src=merged.user_idx.tolist(),
                         tgt=merged.tweet_idx.tolist())
        rel_df = pd.DataFrame(data_dict)
        self.rels[('user', 'posted', 'tweet')] = rel_df

        # (:Tweet)-[:MENTIONS]->(:User)
        mentions_exist = 'entities.mentions' in self.nodes['tweet'].columns
        if self.include_mentions and mentions_exist:
            extract_mention = lambda dcts: [int(dct['id']) for dct in dcts]
            merged = (self.nodes['tweet'][['entities.mentions']]
                          .dropna()
                          .applymap(extract_mention)
                          .explode('entities.mentions')
                          .reset_index()
                          .rename(columns=dict(index='tweet_idx'))
                          .merge(self.nodes['user'][['user_id']]
                                     .reset_index()
                                     .rename(columns=dict(index='user_idx')),
                                 left_on='entities.mentions',
                                 right_on='user_id'))
            data_dict = dict(src=merged.tweet_idx.tolist(),
                             tgt=merged.user_idx.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('tweet', 'mentions', 'user')] = rel_df

        # (:User)-[:MENTIONS]->(:User)
        user_cols = self.nodes['user'].columns
        mentions_exist = 'entities.description.mentions' in user_cols
        if self.include_mentions and mentions_exist:
            extract_mention = lambda dcts: [dct['username'] for dct in dcts]
            merged = (self.nodes['user'][['entities.description.mentions']]
                          .dropna()
                          .applymap(extract_mention)
                          .explode('entities.description.mentions')
                          .reset_index()
                          .rename(columns=dict(index='user_idx1'))
                          .merge(self.nodes['user'][['username']]
                                     .reset_index()
                                     .rename(columns=dict(index='user_idx2')),
                                 left_on='entities.description.mentions',
                                 right_on='username'))
            data_dict = dict(src=merged.user_idx1.tolist(),
                             tgt=merged.user_idx2.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('user', 'mentions', 'user')] = rel_df

        # (:User)-[:HAS_PINNED]->(:Tweet)
        pinned_exist = 'pinned_tweet_id' in self.nodes['user'].columns
        if pinned_exist:
            merged = (self.nodes['user'][['pinned_tweet_id']]
                          .dropna()
                          .reset_index()
                          .rename(columns=dict(index='user_idx'))
                          .merge(self.nodes['tweet'][['tweet_id']]
                                     .reset_index()
                                     .rename(columns=dict(index='tweet_idx')),
                                 left_on='pinned_tweet_id',
                                 right_on='tweet_id'))
            data_dict = dict(src=merged.user_idx.tolist(),
                             tgt=merged.tweet_idx.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('user', 'has_pinned', 'tweet')] = rel_df

        # (:Tweet)-[:LOCATED_IN]->(:Place)
        places_exist = 'geo.place_id' in self.nodes['tweet'].columns
        if self.include_places and places_exist:
            merged = (self.nodes['tweet'][['geo.place_id']]
                          .dropna()
                          .reset_index()
                          .rename(columns=dict(index='tweet_idx'))
                          .merge(self.nodes['place'][['place_id']]
                                     .reset_index()
                                     .rename(columns=dict(index='place_idx')),
                                 left_on='geo.place_id',
                                 right_on='place_id'))
            data_dict = dict(src=merged.tweet_idx.tolist(),
                             tgt=merged.place_idx.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('tweet', 'located_in', 'place')] = rel_df

        # (:Tweet)-[:HAS_POLL]->(:Poll)
        polls_exist = 'attachments.poll_ids' in self.nodes['tweet'].columns
        if self.include_polls and polls_exist:
            merged = (self.nodes['tweet'][['attachments.poll_ids']]
                          .dropna()
                          .explode('attachments.poll_ids')
                          .reset_index()
                          .rename(columns=dict(index='tweet_idx'))
                          .merge(self.nodes['poll'][['poll_id']]
                                     .reset_index()
                                     .rename(columns=dict(index='poll_idx')),
                                 left_on='attachments.poll_ids',
                                 right_on='poll_id'))
            data_dict = dict(src=merged.tweet_idx.tolist(),
                             tgt=merged.poll_idx.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('tweet', 'has_poll', 'poll')] = rel_df

        # (:Tweet)-[:HAS_IMAGE]->(:Image)
        images_exist = 'attachments.media_keys' in self.nodes['tweet'].columns
        if self.include_images and images_exist:
            merged = (self.nodes['tweet'][['attachments.media_keys']]
                          .dropna()
                          .explode('attachments.media_keys')
                          .reset_index()
                          .rename(columns=dict(index='tweet_idx'))
                          .merge(self.nodes['image'][['media_key']]
                                     .reset_index()
                                     .rename(columns=dict(index='image_idx')),
                                 left_on='attachments.media_keys',
                                 right_on='media_key'))
            data_dict = dict(src=merged.tweet_idx.tolist(),
                             tgt=merged.image_idx.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('tweet', 'has_image', 'image')] = rel_df

        # (:Tweet)-[:HAS_HASHTAG]->(:Hashtag)
        hashtags_exist = 'entities.hashtags' in self.nodes['tweet'].columns
        if self.include_hashtags and hashtags_exist:
            def extract_hashtag(dcts: List[dict]) -> List[str]:
                return [dct.get('tag') for dct in dcts]
            merged = (self.nodes['tweet'][['entities.hashtags']]
                          .dropna()
                          .applymap(extract_hashtag)
                          .explode('entities.hashtags')
                          .reset_index()
                          .rename(columns=dict(index='tweet_idx'))
                          .merge(self.nodes['hashtag'][['tag']]
                                     .reset_index()
                                     .rename(columns=dict(index='tag_idx')),
                                 left_on='entities.hashtags',
                                 right_on='tag'))
            data_dict = dict(src=merged.tweet_idx.tolist(),
                             tgt=merged.tag_idx.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('tweet', 'has_hashtag', 'hashtag')] = rel_df

        # (:User)-[:HAS_HASHTAG]->(:Hashtag)
        user_cols = self.nodes['user'].columns
        hashtags_exist = 'entities.description.hashtags' in user_cols
        if self.include_hashtags and hashtags_exist:
            def extract_hashtag(dcts: List[dict]) -> List[str]:
                return [dct.get('tag') for dct in dcts]
            merged = (self.nodes['user'][['entities.description.hashtags']]
                          .dropna()
                          .applymap(extract_hashtag)
                          .explode('entities.description.hashtags')
                          .reset_index()
                          .rename(columns=dict(index='user_idx'))
                          .merge(self.nodes['hashtag'][['tag']]
                                     .reset_index()
                                     .rename(columns=dict(index='tag_idx')),
                                 left_on='entities.description.hashtags',
                                 right_on='tag'))
            data_dict = dict(src=merged.user_idx.tolist(),
                             tgt=merged.tag_idx.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('user', 'has_hashtag', 'hashtag')] = rel_df

        # (:Tweet)-[:HAS_URL]->(:Url)
        urls_exist = 'entities.urls' in self.nodes['tweet'].columns
        if (self.include_articles or self.include_images) and urls_exist:
            def extract_url(dcts: List[dict]) -> List[Union[str, None]]:
                return [dct.get('expanded_url') or dct.get('url')
                        for dct in dcts]
            merged = (self.nodes['tweet'][['entities.urls']]
                          .dropna()
                          .applymap(extract_url)
                          .explode('entities.urls')
                          .reset_index()
                          .rename(columns=dict(index='tweet_idx'))
                          .merge(self.nodes['url'][['url']]
                                     .reset_index()
                                     .rename(columns=dict(index='ul_idx')),
                                 left_on='entities.urls',
                                 right_on='url'))
            data_dict = dict(src=merged.tweet_idx.tolist(),
                             tgt=merged.ul_idx.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('tweet', 'has_url', 'url')] = rel_df

        # (:User)-[:HAS_URL]->(:Url)
        user_cols = self.nodes['user'].columns
        url_urls_exist = 'entities.url.urls' in user_cols
        desc_urls_exist = 'entities.description.urls' in user_cols
        if self.include_images and (url_urls_exist or desc_urls_exist):
            def extract_url(dcts: List[dict]) -> List[Union[str, None]]:
                return [dct.get('expanded_url') or dct.get('url')
                        for dct in dcts]

            # Initialise empty relation, which will be populated below
            rel_df = pd.DataFrame()

            if url_urls_exist:
                merged = (self.nodes['user'][['entities.url.urls']]
                              .dropna()
                              .applymap(extract_url)
                              .explode('entities.url.urls')
                              .reset_index()
                              .rename(columns=dict(index='user_idx'))
                              .merge(self.nodes['url'][['url']]
                                         .reset_index()
                                         .rename(columns=dict(index='ul_idx')),
                                     left_on='entities.url.urls',
                                     right_on='url'))
                data_dict = dict(src=merged.user_idx.tolist(),
                                 tgt=merged.ul_idx.tolist())
                rel_df = rel_df.append(pd.DataFrame(data_dict))

            if desc_urls_exist:
                merged = (self.nodes['user'][['entities.description.urls']]
                              .dropna()
                              .applymap(extract_url)
                              .explode('entities.description.urls')
                              .reset_index()
                              .rename(columns=dict(index='user_idx'))
                              .merge(self.nodes['url'][['url']]
                                         .reset_index()
                                         .rename(columns=dict(index='ul_idx')),
                                     left_on='entities.description.urls',
                                     right_on='url'))
                data_dict = dict(src=merged.user_idx.tolist(),
                                 tgt=merged.ul_idx.tolist())
                rel_df = rel_df.append(pd.DataFrame(data_dict))

            self.rels[('tweet', 'has_url', 'url')] = rel_df

        # (:User)-[:HAS_PROFILE_PICTURE_URL]->(:Url)
        user_cols = self.nodes['user'].columns
        profile_images_exist = 'profile_image_url' in user_cols
        if self.include_images and profile_images_exist:
            merged = (self.nodes['user'][['profile_image_url']]
                          .dropna()
                          .reset_index()
                          .rename(columns=dict(index='user_idx'))
                          .merge(self.nodes['url'][['url']]
                                     .reset_index()
                                     .rename(columns=dict(index='ul_idx')),
                                 left_on='profile_image_url',
                                 right_on='url'))
            data_dict = dict(src=merged.user_idx.tolist(),
                             tgt=merged.ul_idx.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('user', 'has_profile_picture_url', 'url')] = rel_df

    def _extract_articles(self):
        '''Downloads the articles in the dataset'''
        if self.include_articles:

            # Create regex that filters out non-articles. These are common
            # images, videos and social media websites
            non_article_regexs = ['youtu[.]*be', 'vimeo', 'spotify', 'twitter',
                                  'instagram', 'tiktok', 'gab[.]com',
                                  'https://t[.]me', 'imgur', '/photo/',
                                  'mp4', 'mov', 'jpg', 'jpeg', 'bmp', 'png',
                                  'gif', 'pdf']
            non_article_regex = '(' + '|'.join(non_article_regexs) + ')'

            # Filter out the URLs to get the potential article URLs
            article_urls = [url for url in self.nodes['url'].url.tolist()
                            if re.search(non_article_regex, url) is None]

            # Loop over all the Url nodes
            data_dict = defaultdict(list)
            with mp.Pool(processes=mp.cpu_count()) as pool:
                for result in tqdm(pool.imap_unordered(process_article_url,
                                                       article_urls,
                                                       chunksize=5),
                                   desc='Parsing articles',
                                   total=len(article_urls)):

                    # Skip result if URL is not parseable
                    if result is None:
                        continue

                    # Store the data in the data dictionary
                    data_dict['url'].append(result['url'])
                    data_dict['title'].append(result['title'])
                    data_dict['content'].append(result['content'])
                    data_dict['authors'].append(result['authors'])
                    data_dict['publish_date'].append(result['publish_date'])
                    data_dict['top_image_url'].append(result['top_image_url'])

            # Convert the data dictionary to a dataframe and store it as the
            # `Article` node
            article_urls = data_dict.pop('url')
            article_df = pd.DataFrame(data_dict, index=article_urls)
            self.nodes['article'] = article_df

            # Extract top images of the articles
            if self.include_images:

                # Create Url node for each top image url
                urls = article_df.top_image_url.dropna().tolist()
                node_df = pd.DataFrame(dict(url=urls))
                if 'url' in self.nodes.keys():
                    node_df = (self.nodes['url'].append(node_df)
                                                .drop_duplicates()
                                                .reset_index(drop=True))
                self.nodes['url'] = node_df

                # (:Article)-[:HAS_TOP_IMAGE_URL]->(:Url)
                merged = (self.nodes['article'][['top_image_url']]
                              .dropna()
                              .reset_index()
                              .rename(columns=dict(index='article_idx'))
                              .merge(self.nodes['url'][['url']]
                                         .reset_index()
                                         .rename(columns=dict(index='ul_idx')),
                                     left_on='top_image_url',
                                     right_on='url'))
                data_dict = dict(src=merged.article_idx.tolist(),
                                 tgt=merged.ul_idx.tolist())
                rel_df = pd.DataFrame(data_dict)
                self.rels[('article', 'has_top_image_url', 'url')] = rel_df

            # (:Tweet)-[:HAS_ARTICLE]->(:Article)
            merged = (self.rels[('tweet', 'has_url', 'url')]
                          .rename(columns=dict(src='tweet_idx', tgt='ul_idx'))
                          .merge(self.nodes['url'][['url']]
                                     .reset_index()
                                     .rename(columns=dict(index='ul_idx')),
                                 on='ul_idx')
                          .merge(self.nodes['article'][['url']]
                                     .reset_index()
                                     .rename(columns=dict(index='article_idx')),
                                 on='url'))
            data_dict = dict(src=merged.tweet_idx.tolist(),
                             tgt=merged.article_idx.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('tweet', 'has_article', 'article')] = rel_df


    def _extract_images(self):
        '''Downloads the images in the dataset'''
        if self.include_images:

            # Create regex that filters out article urls
            image_urls = [url for url in self.nodes['url'].url.tolist()
                          if url not in self.nodes['article'].url.tolist()]
            image_urls.extend(self.nodes['image'].url.tolist())

            # Loop over all the Url nodes
            data_dict = defaultdict(list)
            with mp.Pool(processes=mp.cpu_count()) as pool:
                for result in tqdm(pool.imap_unordered(process_image_url,
                                                       image_urls,
                                                       chunksize=5),
                                   desc='Parsing images',
                                   total=len(image_urls)):

                    # Skip result if URL is not parseable
                    if result is None:
                        continue

                    # Store the data in the data dictionary
                    data_dict['url'].append(result['url'])
                    data_dict['pixels'].append(result['pixels'])
                    data_dict['height'].append(result['height'])
                    data_dict['width'].append(result['width'])

            # Convert the data dictionary to a dataframe and store it as the
            # `Image` node
            image_urls = data_dict.pop('url')
            image_df = pd.DataFrame(data_dict, index=image_urls)
            self.nodes['image'] = image_df

            # (:Tweet)-[:HAS_IMAGE]->(:Image)
            merged = (self.rels[('tweet', 'has_url', 'url')]
                          .rename(columns=dict(src='tweet_idx', tgt='ul_idx'))
                          .merge(self.nodes['url'][['url']]
                                     .reset_index()
                                     .rename(columns=dict(index='ul_idx')),
                                 on='ul_idx')
                          .merge(self.nodes['image'][['url']]
                                     .reset_index()
                                     .rename(columns=dict(index='image_idx')),
                                 on='url'))
            data_dict = dict(src=merged.tweet_idx.tolist(),
                             tgt=merged.image_idx.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('tweet', 'has_image', 'image')] = rel_df

            # (:Article)-[:HAS_TOP_IMAGE]->(:Image)
            merged = (self.rels[('article', 'has_top_image_url', 'url')]
                          .rename(columns=dict(src='article_idx',
                                               tgt='ul_idx'))
                          .merge(self.nodes['url'][['url']]
                                     .reset_index()
                                     .rename(columns=dict(index='ul_idx')),
                                 on='ul_idx')
                          .merge(self.nodes['image'][['url']]
                                     .reset_index()
                                     .rename(columns=dict(index='image_idx')),
                                 on='url'))
            data_dict = dict(src=merged.article_idx.tolist(),
                             tgt=merged.image_idx.tolist())
            rel_df = pd.DataFrame(data_dict)
            self.rels[('article', 'has_top_image', 'image')] = rel_df

            # (:User)-[:HAS_PROFILE_PICTURE]->(:Image)
            merged = (self.rels[('user', 'has_profile_picture_url', 'url')]
                          .rename(columns=dict(src='user_idx', tgt='ul_idx'))
                          .merge(self.nodes['url'][['url']]
                                     .reset_index()
                                     .rename(columns=dict(index='ul_idx')),
                                 on='ul_idx')
                          .merge(self.nodes['image'][['url']]
                                     .reset_index()
                                     .rename(columns=dict(index='image_idx')),
                                 on='url'))
            data_dict = dict(src=merged.user_idx.tolist(),
                             tgt=merged.image_idx.tolist())
            rel_df = self.rels[rel][is_image_url].reset_index(drop=True)
            self.rels[('user', 'has_profile_picture', 'image')] = rel_df

    def _filter_node_features(self):
        '''Filters the node features to avoid redundancies and noise'''

        # Set up the node features that should be kept
        node_feats = dict(claim=['raw_verdict', 'predicted_verdict',
                                 'reviewer', 'date'],
                          tweet=['tweet_id', 'text', 'created_at', 'lang',
                                 'source', 'public_metrics.retweet_count',
                                 'public_metrics.reply_count',
                                 'public_metrics.quote_count'],
                          user=['user_id', 'verified', 'protected',
                                'created_at', 'username', 'description', 'url',
                                'name', 'public_metrics.followers_count',
                                'public_metrics.following_count',
                                'public_metrics.tweet_count',
                                'public_metrics.listed_count', 'location'],
                          image=['url', 'pixels', 'width', 'height'],
                          article=['url', 'title', 'content'],
                          place=['place_id', 'name', 'full_name',
                                 'country_code', 'country', 'place_type',
                                 'lat', 'lng'],
                          hashtag=['tag'],
                          poll=['poll_id', 'labels', 'votes', 'end_datetime',
                                'voting_status', 'duration_minutes'])

        # Set up renaming of node features that should be kept
        node_feat_renaming = {
            'public_metrics.retweet_count': 'num_retweets',
            'public_metrics.reply_count': 'num_replies',
            'public_metrics.quote_count': 'num_quote_tweets',
            'public_metrics.followers_count': 'num_followers',
            'public_metrics.following_count': 'num_followees',
            'public_metrics.tweet_count': 'num_tweets',
            'public_metrics.listed_count': 'num_listed',
        }

        # Filter and rename the node features
        for node_type, features in node_feats.items():
            if node_type in self.nodes.keys():
                filtered_feats = [feat for feat in features
                                  if feat in self.nodes[node_type].columns]
                renaming_dict = {old: new
                                 for old, new in node_feat_renaming.items()
                                 if old in features}
                self.nodes[node_type] = (self.nodes[node_type][filtered_feats]
                                         .rename(columns=renaming_dict))

    def _remove_auxilliaries(self):
        '''Removes node types that are not in use anymore'''

        # Remove auxilliary node types
        nodes_to_remove = [node_type for node_type in self.nodes.keys()
                          if node_type not in self._node_dump]
        for node_type in nodes_to_remove:
            self.nodes.pop(node_type)

        # Remove auxilliary relation types
        rels_to_remove = [rel_type for rel_type in self.rels.keys()
                          if rel_type not in self._rel_dump]
        for rel_type in rels_to_remove:
            self.rels.pop(rel_type)

    def _dump_to_csv(self):
        '''Dumps the dataset to CSV files'''

        # Dump the nodes
        for node_type in self._node_dump:
            path = self.dataset_dir / f'{node_type}.csv'
            self.nodes[node_type].to_csv(path, index=True)

        # Dump the relations
        for rel_type in self._rel_dump:
            path = self.dataset_dir / f'{"_".join(rel_type)}.csv'
            self.rels[rel_type].to_csv(path, index=False)

    def to_dgl(self,
               output_format: str = 'thread-level-graphs'
               ) -> 'DGLDataset':
        '''Convert the dataset to a DGL dataset.

        Args:
            output_format (str, optional):
                The format the dataset should be outputted in. Can be
                'thread-level-graphs', 'claim-level-graphs' and 'single-graph'.
                Defaults to 'thread-level-graphs'.

        Returns:
            DGLDataset:
                The dataset in DGL format.
        '''
        pass
