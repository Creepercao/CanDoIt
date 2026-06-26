"""Video generation tool — uses provider API if available."""
import httpx
from pathlib import Path
from backend.config import get_provider_by_type

OUTPUT_DIR = Path(__file__).parent.parent.parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)


async def generate_video(
    prompt: str,
    model_id: str = "",
    duration: int = 5,
    width: int = 1024,
    height: int = 576,
) -> dict:
    """Generate video via provider API. Returns {url, local_path, prompt, status}."""
    provider = get_provider_by_type("video") or get_provider_by_type("image")
    if not provider:
        return {"error": "No provider configured", "url": "", "local_path": ""}

    # Try video-specific endpoint
    payload = {
        "model": model_id or "cogvideox-5b",
        "prompt": prompt,
        "duration": duration,
        "width": width,
        "height": height,
    }

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            # Try video generation endpoint
            resp = await client.post(
                f"{provider.base_url}/videos/generations",
                headers={
                    "Authorization": f"Bearer {provider.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                video_url = data.get("url", "") or (data.get("data", [{}])[0].get("url", ""))
                return {"url": video_url, "local_path": "", "prompt": prompt, "status": "done"}
            elif resp.status_code == 404:
                # Video endpoint not available — try text-to-video via chat models that support it
                return await _generate_via_image_endpoint(provider, prompt, model_id)
            else:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}", "url": "", "local_path": ""}
    except Exception as e:
        return {"error": str(e), "url": "", "local_path": ""}


async def _generate_via_image_endpoint(provider, prompt: str, model_id: str) -> dict:
    """Fallback: try generating video frames as images, note limitation."""
    # Many providers don't have native video gen — return status so agent can explain
    return {
        "url": "",
        "local_path": "",
        "prompt": prompt,
        "status": "unsupported",
        "note": "Provider does not support video generation. Use image generation for keyframes."
    }
