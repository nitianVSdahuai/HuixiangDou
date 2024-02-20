# Copyright (c) OpenMMLab. All rights reserved.
"""extract feature and search with user query."""
import argparse
import json
import os
import re
import shutil
from pathlib import Path

import numpy as np
import pytoml
from BCEmbedding.tools.langchain import BCERerank
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.retrievers import ContextualCompressionRetriever
from langchain.text_splitter import (MarkdownHeaderTextSplitter,
                                     MarkdownTextSplitter,
                                     RecursiveCharacterTextSplitter)
from langchain.vectorstores.faiss import FAISS as Vectorstore
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain_core.documents import Document
from loguru import logger
from sklearn.metrics import precision_recall_curve
from torch.cuda import empty_cache


class FeatureStore:
    """Tokenize and extract features from the project's markdown documents, for
    use in the reject pipeline and response pipeline."""

    def __init__(self,
                 device: str = 'cuda',
                 config_path: str = 'config.ini',
                 language: str = 'zh') -> None:
        """Init with model device type and config."""
        self.config_path = config_path
        self.reject_throttle = -1
        self.language = language
        with open(config_path, encoding='utf8') as f:
            config = pytoml.load(f)['feature_store']
            embedding_model_path = config['embedding_model_path']
            reranker_model_path = config['reranker_model_path']
            self.reject_throttle = config['reject_throttle']

        if embedding_model_path is None or len(embedding_model_path) == 0:
            raise Exception('embedding_model_path can not be empty')

        if reranker_model_path is None or len(reranker_model_path) == 0:
            raise Exception('embedding_model_path can not be empty')

        logger.warning(
            '!!! If your feature generated by `text2vec-large-chinese` before 20240208, please rerun `python3 -m huixiangdou.service.feature_store`'  # noqa E501
        )

        logger.debug('loading text2vec model..')
        self.embeddings = HuggingFaceEmbeddings(
            model_name=embedding_model_path,
            model_kwargs={'device': device},
            encode_kwargs={
                'batch_size': 1,
                'normalize_embeddings': True
            })
        self.embeddings.client = self.embeddings.client.half()
        reranker_args = {
            'model': reranker_model_path,
            'top_n': 3,
            'device': device,
            'use_fp16': True
        }
        self.reranker = BCERerank(**reranker_args)
        self.compression_retriever = None
        self.rejecter = None
        self.retriever = None
        self.md_splitter = MarkdownTextSplitter(chunk_size=768,
                                                chunk_overlap=32)
        self.text_splitter = RecursiveCharacterTextSplitter(chunk_size=768,
                                                            chunk_overlap=32)

        self.head_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=[
            ('#', 'Header 1'),
            ('##', 'Header 2'),
            ('###', 'Header 3'),
        ])

    def is_chinese_doc(self, text):
        """If the proportion of Chinese in a bilingual document exceeds 0.5%,
        it is considered a Chinese document."""
        chinese_characters = re.findall(r'[\u4e00-\u9fff]', text)
        total_characters = len(text)
        ratio = 0
        if total_characters > 0:
            ratio = len(chinese_characters) / total_characters
        if ratio >= 0.005:
            return True
        return False

    def cos_similarity(self, v1: list, v2: list):
        """Compute cos distance."""
        num = float(np.dot(v1, v2))
        denom = np.linalg.norm(v1) * np.linalg.norm(v2)
        return 0.5 + 0.5 * (num / denom) if denom != 0 else 0

    def distance(self, text1: str, text2: str):
        """Compute feature distance."""
        feature1 = self.embeddings.embed_query(text1)
        feature2 = self.embeddings.embed_query(text2)
        return self.cos_similarity(feature1, feature2)

    def split_md(self, text: str, source: None):
        """Split the markdown document in a nested way, first extracting the
        header.

        If the extraction result exceeds 1024, split it again according to
        length.
        """
        docs = self.head_splitter.split_text(text)

        final = []
        for doc in docs:
            header = ''
            if len(doc.metadata) > 0:
                if 'Header 1' in doc.metadata:
                    header += doc.metadata['Header 1']
                if 'Header 2' in doc.metadata:
                    header += ' '
                    header += doc.metadata['Header 2']
                if 'Header 3' in doc.metadata:
                    header += ' '
                    header += doc.metadata['Header 3']

            if len(doc.page_content) >= 1024:
                subdocs = self.md_splitter.create_documents([doc.page_content])
                for subdoc in subdocs:
                    if len(subdoc.page_content) >= 10:
                        final.append('{} {}'.format(
                            header, subdoc.page_content.lower()))
            elif len(doc.page_content) >= 10:
                final.append('{} {}'.format(
                    header, doc.page_content.lower()))  # noqa E501

        for item in final:
            if len(item) >= 1024:
                logger.debug('source {} split length {}'.format(
                    source, len(item)))
        return final

    def clean_md(self, text: str):
        """Remove parts of the markdown document that do not contain the key
        question words, such as code blocks, URL links, etc."""
        # remove ref
        pattern_ref = r'\[(.*?)\]\(.*?\)'
        new_text = re.sub(pattern_ref, r'\1', text)

        # remove code block
        pattern_code = r'```.*?```'
        new_text = re.sub(pattern_code, '', new_text, flags=re.DOTALL)

        # remove underline
        new_text = re.sub('_{5,}', '', new_text)

        # remove table
        # new_text = re.sub('\|.*?\|\n\| *\:.*\: *\|.*\n(\|.*\|.*\n)*', '', new_text, flags=re.DOTALL)   # noqa E501

        # use lower
        new_text = new_text.lower()
        return new_text

    def ingress_response(self, markdown_dir: str, work_dir: str):
        """Extract the features required for the response pipeline based on the
        markdown document."""
        feature_dir = os.path.join(work_dir, 'db_response')
        if not os.path.exists(feature_dir):
            os.makedirs(feature_dir)

        ps = list(Path(markdown_dir).glob('**/*.md'))

        documents = []

        for i, p in enumerate(ps):
            logger.debug('{}/{}..'.format(i, len(ps)))
            text = ''
            with open(p, encoding='utf8') as f:
                text = f.read()
                text = self.clean_md(text)
            if len(text) <= 1:
                continue

            chunks = self.split_md(text=text, source=os.path.abspath(p))
            for chunk in chunks:
                new_doc = Document(page_content=chunk,
                                   metadata={'source': os.path.abspath(p)})
                documents.append(new_doc)

        vs = Vectorstore.from_documents(documents, self.embeddings)
        vs.save_local(feature_dir)

    def ingress_reject(self, markdown_dir: str, work_dir: str):
        """Extract the features required for the reject pipeline based on the
        markdown document."""
        feature_dir = os.path.join(work_dir, 'db_reject')
        if not os.path.exists(feature_dir):
            os.makedirs(feature_dir)

        ps = list(Path(markdown_dir).glob('**/*.md'))
        documents = []

        for i, p in enumerate(ps):
            logger.debug('{}/{}..'.format(i, len(ps)))
            text = ''
            with open(p, encoding='utf8') as f:
                text = f.read()
            if len(text) <= 1:
                continue

            chunks = self.split_md(text=text, source=os.path.abspath(p))
            for chunk in chunks:
                new_doc = Document(page_content=chunk,
                                   metadata={'source': os.path.abspath(p)})
                documents.append(new_doc)

        vs = Vectorstore.from_documents(documents, self.embeddings)
        vs.save_local(feature_dir)

    def load_feature(self,
                     work_dir,
                     feature_response: str = 'db_response',
                     feature_reject: str = 'db_reject'):
        """Load extracted feature."""
        # https://api.python.langchain.com/en/latest/vectorstores/langchain.vectorstores.faiss.FAISS.html#langchain.vectorstores.faiss.FAISS

        resp_dir = os.path.join(work_dir, feature_response)
        reject_dir = os.path.join(work_dir, feature_reject)

        if not os.path.exists(resp_dir) or not os.path.exists(reject_dir):
            logger.error(
                'Please check README.md first and `python3 -m huixiangdou.service.feature_store` to initialize feature database'  # noqa E501
            )
            raise Exception(
                f'{resp_dir} or {reject_dir} not exist, please initialize with feature_store.'  # noqa E501
            )

        self.rejecter = Vectorstore.load_local(reject_dir,
                                               embeddings=self.embeddings)
        self.retriever = Vectorstore.load_local(
            resp_dir,
            embeddings=self.embeddings,
            distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT).as_retriever(
                search_type='similarity',
                search_kwargs={
                    'score_threshold': 0.2,
                    'k': 30
                })
        self.compression_retriever = ContextualCompressionRetriever(
            base_compressor=self.reranker, base_retriever=self.retriever)

    def is_reject(self, question, k=20, disable_throttle=False):
        """If no search results below the threshold can be found from the
        database, reject this query."""
        docs = []
        if disable_throttle:
            docs = self.rejecter.similarity_search_with_relevance_scores(
                question, k=1)
        else:
            docs = self.rejecter.similarity_search_with_relevance_scores(
                question, k=k, score_threshold=self.reject_throttle)
        if len(docs) < 1:
            return True, docs
        return False, docs

    def query(self, question: str, context_max_length=16000):
        """Processes a query and returns the best match from the vector store
        database. If the question is rejected, returns None.

        Args:
            question (str): The question asked by the user.

        Returns:
            str: The best matching chunk, or None.
            str: The best matching text, or None
        """
        if question is None or len(question) < 1:
            return None, None

        reject, docs = self.is_reject(question=question)
        if reject:
            return None, None

        docs = self.compression_retriever.get_relevant_documents(question)
        chunks = []
        context = ''
        files = []
        for doc in docs:
            # logger.debug(('db', doc.metadata, question))
            chunks.append(doc.page_content)
            filepath = doc.metadata['source']
            if filepath not in files:
                files.append(filepath)

        # add file content to context, within `context_max_length`
        for idx, doc in enumerate(docs):
            chunk = doc.page_content
            file_text = ''
            with open(doc.metadata['source']) as f:
                file_text = f.read()
            if len(file_text) + len(context) > context_max_length:
                # add and break
                add_len = context_max_length - len(context)
                if add_len <= 0:
                    break
                chunk_index = file_text.find(chunk)
                if chunk_index == -1:
                    # chunk not in file_text
                    context += chunk
                    context += '\n'
                    context += file_text[0:add_len - len(chunk) - 1]
                else:
                    start_index = max(0, chunk_index - (add_len - len(chunk)))
                    context += file_text[start_index:start_index + add_len]
                break
            context += '\n'
            context += file_text

        assert (len(context) <= context_max_length)
        logger.debug('query:{} top1 file:{}'.format(question, files[0]))
        return '\n'.join(chunks), context

    def preprocess(self, repo_dir: str, work_dir: str):
        """Preprocesses markdown files in a given directory excluding those
        containing 'mdb'. Copies each file to 'preprocess' with new name formed
        by joining all subdirectories with '_'.

        Args:
            repo_dir (str): Directory where the original markdown files reside.
            work_dir (str): Working directory where preprocessed files will be stored.  # noqa E501

        Returns:
            str: Path to the directory where preprocessed markdown files are saved.

        Raises:
            Exception: Raise an exception if no markdown files are found in the provided repository directory.  # noqa E501
        """
        markdown_dir = os.path.join(work_dir, 'preprocess')
        if os.path.exists(markdown_dir):
            logger.warning(
                f'{markdown_dir} already exists, remove and regenerate.')
            shutil.rmtree(markdown_dir)
        os.makedirs(markdown_dir)

        # find all .md files except those containing mdb
        mds = []
        for root, _, files in os.walk(repo_dir):
            for file in files:
                if file.endswith('.md') and 'mdb' not in file:
                    mds.append(os.path.join(root, file))

        if len(mds) < 1:
            raise Exception(
                f'cannot search any markdown file, please check usage: python3 {__file__} workdir repodir'  # noqa E501
            )
        for _file in mds:
            tmp = _file.replace('/', '_')
            name = tmp[1:] if tmp.startswith('.') else tmp
            logger.info(name)
            shutil.copy(_file, f'{markdown_dir}/{name}')
        logger.debug(f'preprcessed {len(mds)} files.')
        return markdown_dir

    def initialize(self,
                   repo_dir: str,
                   work_dir: str,
                   config_path: str = 'config.ini',
                   good_questions=[],
                   bad_questions=[]):
        """Initializes response and reject feature store.

        Only needs to be called once. Also calculates the optimal threshold
        based on provided good and bad question examples, and saves it in the
        configuration file.
        """
        logger.info(
            'initialize response and reject feature store, you only need call this once.'  # noqa E501
        )
        markdown_dir = self.preprocess(repo_dir=repo_dir, work_dir=work_dir)
        self.ingress_response(markdown_dir=markdown_dir, work_dir=work_dir)
        self.ingress_reject(markdown_dir=markdown_dir, work_dir=work_dir)

        if len(good_questions) == 0 or len(bad_questions) == 0:
            raise Exception('good and bad question examples cat not be empty.')
        self.load_feature(work_dir=work_dir)
        questions = good_questions + bad_questions
        predictions = []
        for question in questions:
            self.reject_throttle = -1
            _, docs = self.is_reject(question=question, disable_throttle=True)
            score = docs[0][1]
            predictions.append(score)

        labels = [1 for _ in range(len(good_questions))
                  ] + [0 for _ in range(len(bad_questions))]
        precision, recall, thresholds = precision_recall_curve(
            labels, predictions)

        # get the best index for sum(precision, recall)
        sum_precision_recall = precision[:-1] + recall[:-1]
        index_max = np.argmax(sum_precision_recall)
        optimal_threshold = thresholds[index_max]

        with open(config_path, encoding='utf8') as f:
            config = pytoml.load(f)
        config['feature_store']['reject_throttle'] = optimal_threshold
        with open(config_path, 'w', encoding='utf8') as f:
            pytoml.dump(config, f)

        logger.info(
            f'The optimal threshold is: {optimal_threshold}, saved it to {config_path}'  # noqa E501
        )
        empty_cache()


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Feature store for processing directories.')
    parser.add_argument('--work_dir',
                        type=str,
                        default='workdir',
                        help='Working directory.')
    parser.add_argument(
        '--repo_dir',
        type=str,
        default='repodir',
        help='Root directory where the repositories are located.')
    parser.add_argument(
        '--good_questions',
        default='resource/good_questions.json',
        help=  # noqa E251
        'Positive examples in the dataset. Default value is resource/good_questions.json'  # noqa E501
    )
    parser.add_argument(
        '--bad_questions',
        default='resource/bad_questions.json',
        help=  # noqa E251
        'Negative examples json path. Default value is resource/bad_questions.json'  # noqa E501
    )
    parser.add_argument(
        '--config_path',
        default='config.ini',
        help='Feature store configuration path. Default value is config.ini')
    parser.add_argument(
        '--sample', help='Input an json file, save reject and search output.')
    args = parser.parse_args()
    return args


def test_reject(sample: str = None):
    """Simple test reject pipeline."""
    if sample is None:
        real_questions = [
            '请问找不到libmmdeploy.so怎么办',
            'SAM 10个T 的训练集，怎么比比较公平呢~？速度上还有缺陷吧？',
            '想问下，如果只是推理的话，amp的fp16是不会省显存么，我看parameter仍然是float32，开和不开推理的显存占用都是一样的。能不能直接用把数据和model都 .half() 代替呢，相比之下amp好在哪里',  # noqa E501
            'mmdeploy支持ncnn vulkan部署么，我只找到了ncnn cpu 版本',
            '大佬们，如果我想在高空检测安全帽，我应该用 mmdetection 还是 mmrotate',
            'mmdeploy 现在支持 mmtrack 模型转换了么',
            '请问 ncnn 全称是什么',
            '有啥中文的 text to speech 模型吗?',
            '今天中午吃什么？',
            '茴香豆是怎么做的'
        ]
    else:
        with open(sample) as f:
            real_questions = json.load(f)
    fs_query = FeatureStore(config_path=args.config_path)
    fs_query.load_feature(work_dir=args.work_dir)
    for example in real_questions:
        reject, _ = fs_query.is_reject(example)

        if reject:
            logger.error(f'reject query: {example}')
        else:
            logger.warning(f'process query: {example}')

        if sample is not None:
            if reject:
                with open('workdir/negative.txt', 'a+') as f:
                    f.write(example)
                    f.write('\n')
            else:
                with open('workdir/positive.txt', 'a+') as f:
                    f.write(example)
                    f.write('\n')

    del fs_query
    empty_cache()


def test_query(sample: str = None):
    """Simple test response pipeline."""
    if sample is not None:
        with open(sample) as f:
            real_questions = json.load(f)
        logger.add('logs/feature_store_query.log', rotation='4MB')
    else:
        real_questions = ['mmpose installation']

    fs_query = FeatureStore(config_path=args.config_path)
    fs_query.load_feature(work_dir=args.work_dir)
    for example in real_questions:
        example = example[0:400]
        fs_query.query(example)
        empty_cache()

    del fs_query
    empty_cache()


if __name__ == '__main__':
    args = parse_args()

    if args.sample is None:
        # not test precision, build workdir
        fs_init = FeatureStore(config_path=args.config_path)
        with open(args.good_questions, encoding='utf8') as f:
            good_questions = json.load(f)
        with open(args.bad_questions, encoding='utf8') as f:
            bad_questions = json.load(f)

        fs_init.initialize(repo_dir=args.repo_dir,
                           work_dir=args.work_dir,
                           good_questions=good_questions,
                           bad_questions=bad_questions)
        del fs_init

    test_reject(args.sample)
    test_query(args.sample)
