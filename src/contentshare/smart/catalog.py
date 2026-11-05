"""The files smart reading downloads, pinned by address and SHA-256.

A downloaded file is never used unless its SHA-256 matches the one written here, so a changed
or tampered file (or a redirect to somewhere else) is refused. To use a newer program or model,
change the address and the hash together after checking them.

Licences (checked on the models' Hugging Face pages when this list was made):
* Qwen3 4B and Qwen3 1.7B: Apache License 2.0. Free for use in an organisation, with no field
  of use limit; keep the licence text (it is shown in Settings).
* llama.cpp (the program that runs the model): MIT licence.
Other small models are fine to add if their licence is Apache-2.0, MIT or BSD. Models under
"research" or "community" licences (for example Qwen2.5 3B, Llama, Gemma) were left out on
purpose: they limit use by organisations.
"""

from __future__ import annotations

from dataclasses import dataclass

LLAMA_BUILD = "b12086"
_RELEASE = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}"


@dataclass(frozen=True)
class Download:
    name: str  # the file name it is saved under
    url: str
    sha256: str
    size: int  # bytes, for the progress bar and the free-space check
    licence: str


@dataclass(frozen=True)
class ModelChoice:
    key: str
    label: str
    file: Download


LLAMA_CPU = Download(
    name=f"llama-{LLAMA_BUILD}-bin-win-cpu-x64.zip",
    url=f"{_RELEASE}/llama-{LLAMA_BUILD}-bin-win-cpu-x64.zip",
    sha256="bfb33f52394c35a6c1ba3866a0283b094b538c3fe566df4a7259632d757da074",
    size=24_000_000,
    licence="MIT",
)
LLAMA_VULKAN = Download(
    name=f"llama-{LLAMA_BUILD}-bin-win-vulkan-x64.zip",
    url=f"{_RELEASE}/llama-{LLAMA_BUILD}-bin-win-vulkan-x64.zip",
    sha256="04b29146d121bb90b8268fb52a276dadec018dbb6e08b513d859740ef71ff02f",
    size=37_000_000,
    licence="MIT",
)

MODELS: dict[str, ModelChoice] = {
    "recommended": ModelChoice(
        "recommended",
        "Qwen3 4B (about 2.5 GB, best results)",
        Download(
            name="Qwen3-4B-Q4_K_M.gguf",
            url="https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf",
            sha256="7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5",
            size=2_500_000_000,
            licence="Apache-2.0",
        ),
    ),
    "small": ModelChoice(
        "small",
        "Qwen3 1.7B (about 1.8 GB, smaller and faster, less exact)",
        Download(
            name="Qwen3-1.7B-Q8_0.gguf",
            url="https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q8_0.gguf",
            sha256="061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a",
            size=1_830_000_000,
            licence="Apache-2.0",
        ),
    ),
}


def program_for(use_graphics_card: bool) -> Download:
    return LLAMA_VULKAN if use_graphics_card else LLAMA_CPU
