# epub2tts Batch Processor

This adds a batch processing workflow on top of [epub2tts](https://github.com/aedocw/epub2tts):
drop epub files into a folder and get audiobooks out, organized by title and author.

## Folder structure

```
epub/                          ← drop your .epub files here
audiobooks/
  Jade City - Fonda Lee/
    Jade City - Fonda Lee-af_sky.m4b
  Dune - Frank Herbert/
    Dune - Frank Herbert-af_sky.m4b
```

## Running locally (macOS)

Follow the [epub2tts install instructions](README.md) first, then:

```bash
# drop epubs in ./epub/
python3 process_epubs.py
```

Already-processed books (folder already contains a `.m4b`) are skipped automatically.

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `EPUB_DIR` | `epub` | Folder to scan for epubs |
| `AUDIOBOOKS_DIR` | `audiobooks` | Output folder |
| `TTS_ENGINE` | `kokoro` | Engine: `kokoro`, `edge`, `tts`, `xtts`, `openai` |
| `EPUB2TTS_ARGS` | `--skiplinks --skipfootnotes` | Extra flags passed to epub2tts |

## Running with Docker (Linux + NVIDIA GPU)

Requires [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) on the host.

```bash
# Build the image (downloads model weights, takes a few minutes)
docker compose build

# Drop epubs in ./epub/, then run
docker compose run --rm epub2tts
```

The kokoro TTS engine automatically uses CUDA when available, which is significantly faster than CPU.

### Manual docker run

```bash
docker build -f Dockerfile.processor -t epub2tts-processor .

docker run --rm --gpus all \
  -v /path/to/your/epub:/data/epub \
  -v /path/to/your/audiobooks:/data/audiobooks \
  epub2tts-processor
```

## Notes

- The spacy `en_core_web_sm` model is baked into the Docker image to avoid a runtime install issue
- Kokoro model weights (`hexgrad/Kokoro-82M`) are also pre-downloaded at build time
- The default voice is `af_sky` (kokoro American English female)
- To change voice, add `--speaker af_heart` (or any [kokoro voice](https://huggingface.co/hexgrad/Kokoro-82M)) to `EPUB2TTS_ARGS`
