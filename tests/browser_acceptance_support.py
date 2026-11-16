"""Keep opt-in successful journey captures beside the diagnostic failure traces."""
import os
from pathlib import Path


def shot(page, name, *, full_page=True):
    folder = os.getenv('RC_SHOTS')
    if not folder and os.getenv('PW_TRACE_DIR'):
        folder = Path(os.environ['PW_TRACE_DIR']).parent / 'captures'
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / (name + '.png')), full_page=full_page, animations='disabled')
