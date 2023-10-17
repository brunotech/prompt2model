"""An dual-encoder model retriever using HuggingFace model descriptions."""

from __future__ import annotations  # noqa FI58

import json
import os
import tarfile
import urllib.request

import numpy as np
import retriv
import torch
from tqdm import tqdm

from prompt2model.model_retriever.base import ModelRetriever
from prompt2model.model_retriever.generate_hypothetical_document import (
    generate_hypothetical_model_description,
)
from prompt2model.prompt_parser import PromptSpec
from prompt2model.utils import encode_text, retrieve_objects


class ModelInfo:
    """Store the model name, description, and query-model score for each model."""

    def __init__(
        self,
        name: str,
        description: str,
        score: float,
        size_in_bytes: int,
        num_downloads: int,
    ):
        """Initialize a ModelInfo object.

        Args:
            name: The name of the model.
            description: The description of the model.
            score: The similarity of the model to a given prompt from a user.
            size_in_bytes: The size of the model on disk, in bytes.
            num_downloads: The number of downoads for this model on HuggingFace.
        """
        self.name = name
        self.description = description
        self.score = score
        self.size_in_bytes = size_in_bytes
        self.num_downloads = num_downloads


class DescriptionModelRetriever(ModelRetriever):
    """Retrieve a model from among HuggingFace models."""

    def __init__(
        self,
        search_index_path: str | None = None,
        search_depth: int = 5,
        first_stage_depth: int = 1000,
        encoder_model_name: str = "OpenMatch/cocodr-base-msmarco",
        model_descriptions_index_path="huggingface_models/model_info/",
        device: torch.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu"
        ),
        model_size_limit_bytes=3e9,
        use_bm25: bool = True,
        bm25_index_name: str = "model-index",
        use_HyDE: bool = False,
    ):
        """Initialize a dual-encoder retriever against a search index.

        Args:
            search_index_path: Where to store the search index (e.g. encoded vectors).
            search_depth: The number of most-relevant models to retrieve.
            first_stage_depth: The number of models to retrieve purely by similarity,
                before reranking by scaling with model size and number of downloads
                in the past month.
            encoder_model_name: The name of the model to use for the dual-encoder.
            model_descriptions_index_path: The directory of models to search against.
            device: The device to use for encoding text for our dual-encoder model.
            model_size_limit_bytes: The maximum size (in bytes) of a model to retrieve.
            use_bm25: Whether to use BM25 to retrieve the top-k models. If False, we
                use a dual-encoder retriever.
            bm25_index_name: The name used to save the search index for BM25.
            use_HyDE: Whether to use HyDE to replace the query with a hypothetical
                model description generated by an LLM.
        """
        self.search_depth = search_depth
        self.first_stage_depth = first_stage_depth
        self.encoder_model_name = encoder_model_name
        self.model_descriptions_index_path = model_descriptions_index_path
        self.device = device
        self.model_size_limit_bytes = model_size_limit_bytes
        # If use_bm25 is True, then we use BM25 to retrieve the top-k models.
        # Otherwise, we use a dual-encoder retriever.
        self.use_bm25 = use_bm25
        self.use_HyDE = use_HyDE

        # Blocklist certain models' organizations to exclude from model retrieval
        # search results; certain organizations programmatically create models which
        # are unlikely to be useful for task-specific finetuning.
        self.model_blocklist_organizations = ["huggingtweets"]
        self.load_model_info()

        if self.use_bm25:
            if search_index_path is not None:
                raise ValueError(
                    "BM25 expects a search index path with a particular format, "
                    "so search_index_path should not be provided."
                )
            self.bm25_index_name = bm25_index_name
            self._search_index_path = retriv.paths.index_path(self.bm25_index_name)
        else:
            if search_index_path is not None:
                if os.path.isdir(search_index_path):
                    raise ValueError(
                        f"Search index must either be a valid file or not exist yet. "
                        f"But {search_index_path} is provided."
                    )
                self._search_index_path = search_index_path

    @property
    def search_index_path(self):
        """Return the search index path."""
        return self._search_index_path

    def load_model_info(self):
        """Load metadata (e.g. downloads, publication date) about various models.

        We load metadata from json files in the model_descriptions_index_path
        directory, filter out models from certain organizations, and initialize a list
        of ModelInfo objects corresponding to the models we want to search against.
        """
        if not os.path.isdir(self.model_descriptions_index_path):
            # If the model descriptions directory is not populated, then populate it.
            urllib.request.urlretrieve(
                "http://phontron.com/data/prompt2model/model_info.tgz",
                "/tmp/model_info.tgz",
            )
            tar = tarfile.open("/tmp/model_info.tgz")
            os.makedirs(self.model_descriptions_index_path)
            tar.extractall(path=self.model_descriptions_index_path)

        description_files = os.listdir(self.model_descriptions_index_path)
        # We store model names and descriptions in a list of ModelInfo objects.
        self.model_infos: list[ModelInfo] = []
        for f in tqdm(description_files):
            if (
                f.startswith(".")
                or len(open(os.path.join(self.model_descriptions_index_path, f)).read())
                == 0
            ):
                continue
            block = any(
                f.startswith(f"{org}/")
                for org in self.model_blocklist_organizations
            )
            if block:
                continue
            model_dict = json.load(
                open(os.path.join(self.model_descriptions_index_path, f))
            )
            if model_dict.get("size_bytes", 0) == 0:
                continue
            if "description" not in model_dict:
                continue
            model_name = model_dict["pretrained_model_name"]
            model_info = ModelInfo(
                name=model_name,
                description=model_dict["description"],
                score=None,
                size_in_bytes=model_dict["size_bytes"],
                num_downloads=model_dict.get("downloads", 0),
            )
            self.model_infos.append(model_info)

    def encode_model_descriptions(self, search_index_path) -> np.ndarray:
        """Encode model descriptions into a vector for indexing."""
        model_descriptions = [model.description for model in self.model_infos]
        return encode_text(
            self.encoder_model_name,
            text_to_encode=model_descriptions,
            encoding_file=search_index_path,
            device=self.device,
        )

    def scale_similarity_score(
        self, model_info: ModelInfo, model_score: float
    ) -> float:
        """Adjust the search score using the model size and number of downloads.

        Args:
            model_info: The name of the model we are scoring.
            model_score: The similarity score of this model for this particular query.
        """
        num_downloads = int(model_info.num_downloads)
        # By taking the log of the number of downloads plus 2, we avoid zeroing the
        # score of models with 0 downloads.
        log_num_downloads = np.log10(num_downloads + 2)
        model_size_bytes = int(model_info.size_in_bytes)
        if model_size_bytes > self.model_size_limit_bytes:
            return -np.inf
        return model_score * log_num_downloads

    def bm25_index_exists(self):
        """Check if a BM25 index exists."""
        if not self.use_bm25:
            raise ValueError("BM25 is not enabled.")
        return (
            os.path.exists(self._search_index_path)
            and len(os.listdir(self._search_index_path)) > 0
        )

    def construct_bm25_index(self, model_infos):
        """Construct a retriv BM25 index for model descriptions."""
        collection = [
            {"id": model.name, "text": model.description} for model in model_infos
        ]
        if self.bm25_index_exists():
            search_engine = retriv.SparseRetriever.load(self._search_index_path)
        else:
            search_engine = retriv.SparseRetriever(self.bm25_index_name)
            search_engine.index(collection)
        return search_engine

    def retrieve(
        self,
        prompt: PromptSpec,
    ) -> list[str]:
        """Select a model from a prompt using a dual-encoder retriever.

        Args:
            prompt: A prompt whose instruction field we use to select relevant models.

        Return:
            A list of relevant models' HuggingFace names.
        """
        if (
            not self.use_bm25
            and self._search_index_path is not None
            and not os.path.exists(self._search_index_path)
        ):
            self.encode_model_descriptions(self._search_index_path)

        if self.use_HyDE:
            query_text = generate_hypothetical_model_description(prompt)
        else:
            query_text = prompt.instruction

        if self.use_bm25:
            search_engine = self.construct_bm25_index(self.model_infos)
            results = search_engine.search(query_text, cutoff=self.first_stage_depth)
            ranked_list: list[tuple[str, float]] = [
                (result["id"], result["score"]) for result in results
            ]
        else:
            query_vector = encode_text(
                self.encoder_model_name,
                text_to_encode=query_text,
                device=self.device,
            )
            model_names = [model.name for model in self.model_infos]
            ranked_list = retrieve_objects(
                query_vector,
                self._search_index_path,
                model_names,
                self.first_stage_depth,
            )

        model_name_to_model_info = {
            model_info.name: model_info for model_info in self.model_infos
        }

        top_models_list = []
        for model_name, model_score in ranked_list:
            model_info = model_name_to_model_info[model_name]
            scaled_model_score = self.scale_similarity_score(model_info, model_score)
            model_info.score = scaled_model_score
            top_models_list.append(model_info)

        top_models_list = sorted(top_models_list, key=lambda x: x.score, reverse=True)[
            : self.search_depth
        ]
        if len(top_models_list) == 0:
            raise ValueError("No models retrieved from search index.")
        return [model_info.name for model_info in top_models_list]
