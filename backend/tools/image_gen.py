"""Image generation tool using SiliconFlow or compatible API."""
import httpx
import base64
from pathlib import Path
from backend.config import PROVIDERS


OUTPUT_DIR = Path(__file__).parent.parent.parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)


async def generate_image(
    prompt: str,
    model_id: str = "stabilityai/stable-diffusion-3-5-large",
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
    steps: int = 20,
    seed: int = -1,
) -> dict:
    """Generate image via provider API. Returns {url, local_path, prompt}."""
    provider = PROVIDERS[0] if PROVIDERS else None
    if not provider:
        return {"error": "No provider configured", "url": "", "local_path": ""}

    payload = {
        "model": model_id,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": width,
        "height": height,
        "steps": steps,
    }
    if seed >= 0:
        payload["seed"] = seed

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{provider.base_url}/images/generations",
                headers={
                    "Authorization": f"Bearer {provider.apikey}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                # OpenAI-compatible image response
                if "data" in data and len(data["data"]) > 0:
                    img_url = data["data"][0].get("url", "")
                    b64 = data["data"][0].get("b64_json", "")
                    local_path = ""
                    if b64:
                        local_path = str(OUTPUT_DIR / f"img_{hash(prompt) & 0xFFFF}.png")
                        with open(local_path, "wb") as f:
                            f.write(base64.b64decode(b64))
                    return {"url": img_url, "local_path": local_path, "prompt": prompt}
                return {"url": str(data), "local_path": "", "prompt": prompt}
            else:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}", "url": "", "local_path": ""}
    except Exception as e:
        return {"error": str(e), "url": "", "local_path": ""}
