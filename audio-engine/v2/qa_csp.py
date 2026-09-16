"""Run the existing UI tests without weakening the application's CSP."""
from pathlib import Path
import json, runpy, time, traceback
from playwright.sync_api import Page

OUT = Path(__file__).resolve().parents[2] / 'qa-results'
OUT.mkdir(exist_ok=True)

def wait_expression(self, expression, *, timeout=30000, arg=None, polling=None):
    deadline = time.monotonic() + (timeout or 30000) / 1000
    while time.monotonic() < deadline:
        # CDP/WebKit evaluation compiles this expression directly. The original
        # wait_for_function helper instead calls eval inside the page.
        if self.evaluate('() => Boolean(' + expression + ')'):
            return None
        self.wait_for_timeout(100)
    try:
        self.screenshot(path=str(OUT / 'timeout.png'), full_page=True)
        (OUT / 'timeout-page.html').write_text(self.content())
        (OUT / 'timeout-state.json').write_text(json.dumps(self.evaluate('''() => ({
          title: document.title,
          text: document.body.innerText,
          audio: document.querySelector('audio') ? {
            readyState: document.querySelector('audio').readyState,
            paused: document.querySelector('audio').paused,
            currentTime: document.querySelector('audio').currentTime,
            error: document.querySelector('audio').error?.message
          } : null
        })'''), indent=2))
    except Exception:
        pass
    raise TimeoutError('Browser assertion timed out: ' + expression)

Page.wait_for_function = wait_expression
try:
    runpy.run_path(str(Path(__file__).with_name('qa.py')), run_name='__main__')
except Exception:
    (OUT / 'failure.txt').write_text(traceback.format_exc())
    raise
