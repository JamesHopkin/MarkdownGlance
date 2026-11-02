import html
import string
import threading
from typing import Callable, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

import sublime

# Proportional tables (ADR 0024) are laid out from how wide the preview draws
# each character, and only minihtml knows that. It is measured in a preview
# that is on screen -- a hidden panel never lays its phantoms out -- in lines
# added below the document, one inline phantom each, read back on the next
# tick and removed again. Nothing measured is visible: the lines are below the
# document and the text in them is transparent.
PHANTOMS = "mdglance-measure"
REPEAT = 10
# Measured with the first request for a font, size and style, so that one pass
# covers most documents rather than one per new character.
BASE = frozenset(string.ascii_letters + string.digits + string.punctuation + " ")
NBSP = " "
TRANSPARENT = "#00000000"

Key = Tuple[str, int, str]
Job = Tuple[Key, str, int]


def _markup(style: str, text: str) -> str:
    if "c" in style:
        text = "<code>{}</code>".format(text)
    if "i" in style:
        text = '<span style="font-style: italic">{}</span>'.format(text)
    if "b" in style:
        text = '<span style="font-weight: bold">{}</span>'.format(text)
    return text


def measurement_html(base_css: str, key: Key, char: str, count: int) -> str:
    """One string, drawn the way a proportional table draws its text.

    Inside `.md-table`, so that its rules apply -- `code` loses its padding
    there -- with the box's own margin, padding and border taken off: they
    would cancel out of the reading anyway, and this keeps it small.
    """
    family, font_px, style = key
    text = html.escape(char * count).replace(" ", NBSP)
    box = (
        "margin: 0; padding: 0; border: 0; background-color: {}; "
        "font-family: {}; font-size: {}px"
    ).format(TRANSPARENT, family, font_px)
    return (
        "<style>{}</style>"
        '<body style="margin: 0; padding: 0">'
        '<div class="md-table" style="{}">'
        '<span style="white-space: nowrap; color: {}">{}</span>'
        "</div></body>"
    ).format(base_css, html.escape(box, quote=True), TRANSPARENT, _markup(style, text))


class TableMeasurer:
    """The `TextMeasurer` a render is given, and the work behind it.

    Called on a render worker. A font, size and style it has fully measured is
    answered from the cache; anything else is queued, measured on the UI thread
    in whichever preview is on screen, and `on_measured` asks for the renders
    that were set in monospace meanwhile.
    """

    def __init__(
        self,
        surface: Callable[[], Optional[object]],
        base_css: str,
        on_measured: Callable[[], None],
        trim_command: str = "mdglance_trim_surface",
    ) -> None:
        self._surface = surface
        self._base_css = base_css
        self._on_measured = on_measured
        self._trim_command = trim_command
        self._lock = threading.Lock()
        self._known: Dict[Key, Dict[str, float]] = {}
        self._wanted: Dict[Key, Set[str]] = {}
        self._scheduled = False
        self._busy = False

    def __call__(
        self, family: str, font_px: int, style: str, chars: FrozenSet[str]
    ) -> Optional[Mapping[str, float]]:
        key = (family, font_px, style)
        with self._lock:
            known = self._known.get(key, {})
            missing = set(chars) - set(known)
            if not missing:
                return {char: known[char] for char in chars}
            wanted = self._wanted.setdefault(key, set())
            if key not in self._known:
                wanted.update(BASE)
            wanted.update(missing)
            if not self._scheduled and not self._busy:
                self._scheduled = True
                sublime.set_timeout(self._start, 0)
            return None

    def _start(self) -> None:
        with self._lock:
            self._scheduled = False
            if self._busy or not self._wanted:
                return
            view = self._surface()
            if view is None:
                # No preview on screen to measure in. The next render asks
                # again, and that render is the one a visible preview makes.
                return
            wanted, self._wanted = self._wanted, {}
            self._busy = True
        jobs: List[Job] = [
            (key, char, count)
            for key in sorted(wanted)
            for char in sorted(wanted[key])
            for count in (REPEAT, 2 * REPEAT)
        ]
        base = view.size()
        try:
            self._edit(
                view,
                lambda: view.run_command(
                    "append",
                    {
                        "characters": "\n" + " \n" * len(jobs),
                        "force": True,
                        "scroll_to_end": False,
                    },
                ),
            )
            phantoms = sublime.PhantomSet(view, PHANTOMS)
            phantoms.update(
                [
                    sublime.Phantom(
                        sublime.Region(base + 1 + 2 * line),
                        measurement_html(self._base_css, key, char, count),
                        sublime.LAYOUT_INLINE,
                    )
                    for line, (key, char, count) in enumerate(jobs)
                ]
            )
        except Exception:
            self._finish(view, base, None, wanted, None)
            raise
        sublime.set_timeout(lambda: self._read(view, base, jobs, wanted, phantoms), 0)

    def _read(self, view, base: int, jobs: List[Job], wanted, phantoms) -> None:
        measured: Optional[Dict[Key, Dict[str, float]]] = None
        try:
            # Where each line's space ends: the phantom before it pushes it
            # right by exactly the phantom's width, and the difference between
            # ten copies and twenty cancels everything but the characters.
            ends = {
                job: view.text_to_layout(base + 2 + 2 * line)[0]
                for line, job in enumerate(jobs)
            }
            result: Dict[Key, Dict[str, float]] = {}
            for (key, char, count), end in ends.items():
                if count == REPEAT:
                    longer = ends[(key, char, 2 * REPEAT)]
                    result.setdefault(key, {})[char] = (longer - end) / REPEAT
            # A preview that was not laid out answers zero for everything.
            if all(
                widths.get(" ", 0) > 0 and widths.get("e", 1) > 0
                for widths in result.values()
            ):
                measured = result
        finally:
            self._finish(view, base, phantoms, wanted, measured)

    def _finish(self, view, base: int, phantoms, wanted, measured) -> None:
        if view.is_valid():
            if phantoms is not None:
                phantoms.update([])
            if view.size() > base:
                self._edit(
                    view,
                    lambda: view.run_command(self._trim_command, {"size": base}),
                )
        with self._lock:
            self._busy = False
            if measured is None:
                for key, chars in wanted.items():
                    self._wanted.setdefault(key, set()).update(chars)
            else:
                for key, widths in measured.items():
                    self._known.setdefault(key, {}).update(widths)
            again = bool(self._wanted) and measured is not None
            if again and not self._scheduled:
                self._scheduled = True
                sublime.set_timeout(self._start, 0)
        if measured is not None:
            self._on_measured()

    @staticmethod
    def _edit(view, change: Callable[[], None]) -> None:
        """A preview is read-only to the reader; this is the package's own
        edit, made and undone on one surface while it is on screen."""
        read_only = view.is_read_only()
        view.set_read_only(False)
        try:
            change()
        finally:
            view.set_read_only(read_only)
