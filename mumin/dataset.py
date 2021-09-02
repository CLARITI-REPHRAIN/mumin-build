'''Script containing the main dataset class'''

from pathlib import Path
from typing import Union, Dict, Tuple, List
import pandas as pd
import logging
import requests
import zipfile
import io

from .twitter import Twitter


logger = logging.getLogger(__name__)


class MuminDataset:
    '''The MuMiN misinformation dataset, from [1].

    Args:
        twitter_bearer_token(str):
            The Twitter bearer token.
        size (str, optional):
            The size of the dataset. Can be either 'small', 'medium' or
            'large'. Defaults to 'large'.
        dataset_dir (str or pathlib Path, optional):
            The path to the folder where the dataset should be stored. Defaults
            to './mumin'.

    Attributes:
        twitter (Twitter object): A wrapper for the Twitter API.
        size (str): The size of the dataset.
        dataset_dir (pathlib Path): The dataset directory.
        nodes (dict): The nodes of the dataset.
        rels (list): The relations of the dataset.

    References:
        - [1] Nielsen and McConville: _MuMiN: A Large-Scale Multilingual
              Multimodal Fact-Checked Misinformation Dataset with Linked Social
              Network Posts_ (2021)
    '''

    download_url: str = ('https://github.com/CLARITI-REPHRAIN/mumin-build/'
                         'tree/main/data/mumin.zip')

    def __init__(self,
                 twitter_bearer_token: str,
                 size: str = 'large',
                 dataset_dir: Union[str, Path] = './mumin'):
        self.twitter = Twitter(twitter_bearer_token=twitter_bearer_token)
        self.size = size
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

    def compile(self):
        '''Compiles the dataset.

        This entails downloading the dataset, rehydrating the Twitter data and
        downloading the relevant associated data, such as articles, images and
        videos.
        '''
        self._download()
        self._load_dataset()
        self._rehydrate()
        self._extract_twitter_data()
        self._populate_articles()
        self._populate_media()
        self._dump_to_csv()

    def _download(self):
        '''Downloads and unzips the dataset'''
        response = requests.get(self.download_url)

        # If the response was unsuccessful then raise an error
        if response.status_code != 200:
            raise RuntimeError(f'[{response.status_code}] {response.content}')

        # Otherwise unzip the in-memory zip file to `self.dataset_dir`
        else:
            zipped = response.raw.read()
            with zipfile.ZipFile(io.BytesIO(zipped)) as zip_file:
                zip_file.extractall(self.dataset_dir)

    def _load_dataset(self):
        '''Loads the dataset files into memory'''

        # Create the dataset directory if it does not already exist
        if not self.dataset_dir.exists():
            self.dataset_dir.mkdir()

        # Loop over the files in the dataset directory
        for path in self.dataset_dir.iterdir():
            fname = path.stem

            # Node case: no underscores in file name
            if len(fname.split('_')) == 0:
                self.nodes[fname] = pd.DataFrame(pd.read_csv(path))

            # Relation case: exactly two underscores in file name
            elif len(fname.split('_')) == 2:
                src, rel, tgt = tuple(fname.split('_'))
                self.rels[(src, rel, tgt)] = pd.DataFrame(pd.read_csv(path))

            # Otherwise raise error
            else:
                raise RuntimeError(f'Could not recognise {fname} as a node '
                                   f'or relation.')

        # Ensure that tweets are present in the dataset, and also that the
        # tweet IDs are unique
        if 'tweet' not in self.nodes.keys():
            raise RuntimeError('No tweets are present in the zipfile!')
        else:
            tweet_df = self.nodes['tweet']
            duplicate_tweet_ids = tweet_df.id.duplicated().tolist()
            if len(duplicate_tweet_ids) > 0:
                raise RuntimeError(f'The tweet IDs {duplicate_tweet_ids} are '
                                   f'duplicate in the dataset!')

        # Ensure that users are present in the dataset, and also that the
        # user IDs are unique
        if 'user' not in self.nodes.keys():
            raise RuntimeError('No users are present in the zipfile!')
        else:
            user_df = self.nodes['user']
            duplicate_user_ids = user_df.id.duplicated().tolist()
            if len(duplicate_user_ids) > 0:
                raise RuntimeError(f'The user IDs {duplicate_user_ids} are '
                                   f'duplicate in the dataset!')

    def _rehydrate(self):
        '''Rehydrate the tweets and users in the dataset'''

        # Ensure that the tweet and user IDs have been loaded into memory
        if 'tweet' not in self.nodes.keys():
            raise RuntimeError('Tweet IDs have not been loaded yet! '
                               'Load the dataset first.')
        elif 'user' not in self.nodes.keys():
            raise RuntimeError('User IDs have not been loaded yet! '
                               'Load the dataset first.')
        else:
            # Get the tweet and user IDs
            tweet_ids = self.nodes['tweet'].tweet_id.tolist()
            user_ids = self.nodes['user'].user_id.tolist()

            # Rehydrate the tweets and users
            tweet_dfs = self.twitter.rehydrate_tweets(tweet_ids=tweet_ids)
            user_dfs = self.twitter.rehydrate_users(user_ids=user_ids)

            # Ensure that all the users associated to the tweets are already
            # among the user IDs
            existing_users = set(user_dfs['users'].id)
            related_users = set(tweet_dfs['users'].id)
            missing_users = related_users.difference(existing_users)
            if len(missing_users) > 0:
                raise RuntimeError(f'There are {len(missing_users)} users '
                                   f'associated with the tweet IDs which were '
                                   f'not included in the user IDs!')

            # Merge the tweet dataframe with the tweet ID dataframe, to
            # preserve the node IDs
            tweet_df = self.nodes['tweet'].merge(tweet_dfs['tweets'],
                                                 left_on='tweet_id',
                                                 right_on='id')
            self.nodes['tweet'] = tweet_df

            # Merge the user dataframe with the user ID dataframe, to
            # preserve the node IDs
            user_df = self.nodes['user'].merge(user_dfs['users'],
                                               left_on='user_id',
                                               right_on='id')
            self.nodes['user'] = user_df

            # Extract and store the other node types
            self.nodes['media'] = tweet_dfs['media']
            self.nodes['poll'] = tweet_dfs['polls']
            self.nodes['place'] = tweet_dfs['places']

    def _extract_twitter_data(self):
        '''Extracts data from the raw Twitter data'''
        pass

    def _populate_articles(self):
        '''Downloads the articles in the dataset'''
        pass

    def _populate_media(self):
        '''Downloads the images and videos in the dataset'''
        pass

    def _dump_to_csv(self):
        '''Dumps the dataset to CSV files'''
        pass

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

    def to_pyg(self,
               output_format: str = 'thread-level-graphs'
               ) -> 'InMemoryDataset':
        '''Convert the dataset to a PyTorch Geometric dataset.

        Args:
            output_format (str, optional):
                The format the dataset should be outputted in. Can be
                'thread-level-graphs', 'claim-level-graphs' and 'single-graph'.
                Defaults to 'thread-level-graphs'.

        Returns:
            PyTorch Geometric InMemoryDataset:
                The dataset in PyTorch Geometric format.
        '''
        pass
