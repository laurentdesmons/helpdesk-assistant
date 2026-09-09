"""Text embeddings via the Foundry project's OpenAI-compatible endpoint.

Pure constructor — no client is built and no network call is made until
:meth:`Embedder.embed` runs. The embedding deployment name is
``Settings.embedding_model`` (portal-managed on the Foundry project, same as the
classifier's chat model).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from helpdesk.config import Settings

if TYPE_CHECKING:
    from azure.ai.projects.aio import AIProjectClient
    from azure.identity.aio import DefaultAzureCredential
    from openai import AsyncOpenAI

logger = logging.getLogger("helpdesk.search.embeddings")

_MAX_BATCH = 64


def _account_openai_v1_url(project_endpoint: str) -> str:
    """Account-level ``/openai/v1`` base URL for a Foundry project endpoint.

    The project-scoped ``.../api/projects/<name>/openai/v1`` passthrough proxies
    chat/responses but NOT embeddings — those must hit the account-level route.
    """
    parts = urlsplit(project_endpoint)
    return urlunsplit((parts.scheme, parts.netloc, "/openai/v1", "", ""))


class Embedder:
    def __init__(self, settings: Settings) -> None:
        settings.require("foundry_project_endpoint")
        assert settings.foundry_project_endpoint is not None  # narrowed by require()
        self._settings = settings
        self._endpoint = settings.foundry_project_endpoint
        self._base_url = _account_openai_v1_url(settings.foundry_project_endpoint)
        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._project: AIProjectClient | None = None
        self._credential: DefaultAzureCredential | None = None
        self._client: AsyncOpenAI | None = None

    async def _openai(self) -> AsyncOpenAI:
        if self._client is None:
            from azure.ai.projects.aio import AIProjectClient
            from azure.identity.aio import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
            self._project = AIProjectClient(endpoint=self._endpoint, credential=self._credential)
            # Override the project-scoped base URL — embeddings only route on the
            # account-level /openai/v1 endpoint (see _account_openai_v1_url).
            self._client = self._project.get_openai_client(base_url=self._base_url)
            logger.debug(
                "embedder: base_url=%s model=%s dims=%s",
                self._base_url,
                self._model,
                self._dimensions,
            )
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        client = await self._openai()
        out: list[list[float]] = []
        for start in range(0, len(texts), _MAX_BATCH):
            batch = texts[start : start + _MAX_BATCH]
            resp = await client.embeddings.create(
                model=self._model, input=batch, dimensions=self._dimensions
            )
            out.extend(item.embedding for item in resp.data)
        return out

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    async def aclose(self) -> None:
        if self._project is not None:
            await self._project.close()
        if self._credential is not None:
            await self._credential.close()
        self._project = self._credential = self._client = None
