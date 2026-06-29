# Agetha.exe

Desktop AI companion with a Win95 aesthetic. PNG tuber with GPT/LLM backend.

> **⚠ Status: Under active refactoring**
> 
> Removing dangerous features that allowed arbitrary code execution through LLM prompt injection.

## What's removed

- **`run_command`** — RCE vector. LLM could execute any shell command. **Gone.**
- `force_close` — remains but restricted
- File operations (`create_file`, `delete_file`, `rename_file`, etc.) — remain but sanitized against path traversal

## Setup

```bash
pip install pillow pyautogui pytesseract numpy pygame-ce requests pywin32 SpeechRecognition pyaudio openai
```

1. Edit `config.txt` — set `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`
2. Run `python main.py`

## Requirements

- Python 3.11+
- Tesseract OCR (for screen reading)
- PyAudio (for microphone input)
