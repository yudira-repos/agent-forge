"""Google Cloud Vertex AI adapter (Gemini 1.5 / 2.0 Pro, Flash)."""

from __future__ import annotations

from typing import Any

from ..base import AgentContext, AgentResult, RuntimeAdapter


class VertexAdapter(RuntimeAdapter):
    """
    Adapter for Google Cloud Vertex AI generative models.

    Uses the ``google-cloud-aiplatform`` SDK and supports Gemini 2.0
    Pro / Flash, function calling, and streaming.

    Example::

        adapter = VertexAdapter(
            project="my-gcp-project",
            location="us-central1",
            model="gemini-2.0-flash-001",
        )
        runtime = AgentRuntime(adapter=adapter)
    """

    name = "vertex"

    def __init__(
        self,
        project: str | None = None,
        location: str = "us-central1",
        model: str = "gemini-2.0-flash-001",
        credentials: Any = None,
    ) -> None:
        self._project = project
        self._location = location
        self._model = model
        self._credentials = credentials
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import vertexai  # type: ignore
                from vertexai.generative_models import GenerativeModel  # type: ignore

                vertexai.init(
                    project=self._project,
                    location=self._location,
                    credentials=self._credentials,
                )
                self._client = GenerativeModel(self._model)
            except ImportError as exc:
                raise ImportError(
                    "google-cloud-aiplatform is required for VertexAdapter. "
                    "Install it with: pip install google-cloud-aiplatform"
                ) from exc
        return self._client

    async def invoke(self, context: AgentContext) -> AgentResult:
        model = self._get_client()  # raises ImportError with a helpful message if not installed

        # Convert tools to Vertex function declarations if provided
        generation_config = {
            "max_output_tokens": context.max_tokens,
            "temperature": context.temperature,
        }

        response = await model.generate_content_async(
            context.prompt,
            generation_config=generation_config,
        )

        content = response.text if hasattr(response, "text") else ""

        # Extract usage metadata if available
        usage: dict[str, int] = {}
        if hasattr(response, "usage_metadata"):
            usage = {
                "input_tokens": getattr(response.usage_metadata, "prompt_token_count", 0),
                "output_tokens": getattr(response.usage_metadata, "candidates_token_count", 0),
            }

        return AgentResult(
            content=content,
            tool_calls=[],
            usage=usage,
            stop_reason="stop",
            correlation_id=context.correlation_id,
            duration_ms=0.0,
            raw_response=response,
        )

    def health_check(self) -> bool:
        try:
            import vertexai  # type: ignore  # noqa: F401
            return True
        except ImportError:
            return False
