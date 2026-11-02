"""Rewrite table trees into aligned rows: monospace, or proportional.

minihtml implements no ``<table>`` layout and no ``width`` property, so the host
cannot lay a table out and the renderer has to do the typesetting itself.
Measured on ST 4200 (Linux, X11): a monospace block keeps a constant advance for
plain, bold, ``<code>`` and link runs, and for East Asian characters at two
columns each; runs of plain spaces collapse but U+00A0 does not. Columns are
therefore padded to a fixed character width with U+00A0. Emoji come from a
colour font whose advance is not exactly two columns, so a cell holding one
can be a fraction of a character out.

A table wider than the preview would be re-wrapped by minihtml, which destroys
the alignment, so it is wrapped here instead: a budget of characters is shared
out over the columns, each cell is broken into as many lines as it needs, and
every physical line is padded on its own. A cell whose content cannot be
measured (an image) counts as zero columns.

The budget comes from the measured preview width, so a table fills the group it
is in and re-fits when the window is resized. `budgets()` converts pixels to
characters for both fonts the stylesheet can pick; when the width is not known
yet it falls back to a width that suits a narrow group.

With `table_font_family` set, the same layout runs in pixels instead (ADR
0024): widths come from a measurer that asks the host how wide each character
is drawn, and each cell is held at its column's width by a spacer image rather
than padded with U+00A0. Until the measurer knows every character a render
needs, its tables are set in monospace as before.
"""

import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import (
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from .model import ElementNode, Node, TextNode

NBSP = "\u00a0"
COLUMN_GAP = 2
MIN_COLUMN = 3
CELL_TAGS = frozenset(("td", "th"))
SECTION_TAGS = frozenset(("thead", "tbody", "tfoot"))
DROPPED_CELL_TAGS = frozenset(("br",))
WIDE_WIDTHS = frozenset(("W", "F"))

# Advance of one character as a fraction of the font size, for the two stacks
# in the stylesheet: DejaVu Sans Mono, and Noto Sans Mono CJK for a CJK table.
# minihtml rounds the advance up to whole pixels — measured 10 px, not 9.64,
# at a 16 px root — so `budgets` rounds the same way rather than overshooting.
LATIN_ADVANCE = 0.6023
CJK_ADVANCE = 0.5
# The CJK stack names fonts that a Linux desktop installs with its CJK support
# and that neither Windows nor macOS ships. Without one the table is set in
# `monospace` -- Consolas, Menlo, Courier -- at no more than the Latin advance,
# and a budget counted at 0.5 em overflows the preview by a fifth.
CJK_FONT_SHIPS = sys.platform.startswith("linux")
# Body padding (1.5rem each side), table padding (1rem) and border (0.08rem).
# All in rem, so `table_font_scale` leaves them where they are.
CHROME_REM = 5.16
# `.md-table { line-height }` in `resources/preview.css`, scaled with the font.
LINE_REM = 1.45
# One column for the scrollbar and for rounding in the host's own measurement.
SAFETY_COLUMNS = 1
FALLBACK_COLUMNS = 48

# Proportional tables (ADR 0024). A cell is an inline-block held open by a
# transparent image stretched to the column's width, so it is exactly as wide
# as its column whatever its text measures -- provided the text is no wider.
# The widths are the host's own, measured per character; they are summed and
# rounded up with a margin, because a word's real width also carries kerning.
SPACER = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)
MEASURE_SAFETY = 1.05
# Gap between columns and the narrowest column, in ems of the table's font.
PROPORTIONAL_GAP_EM = 1.0
PROPORTIONAL_MIN_EM = 2.0
# What a character averages, for the width the setting's cap stands for.
AVERAGE_EM = 0.5

# A measurer answers for one font, size and style -- "" for regular text, then
# any of "b" bold, "i" italic and "c" code -- with a width for every character
# asked about, or None while any of them is still unknown.
TextMeasurer = Callable[[str, int, str, FrozenSet[str]], Optional[Mapping[str, float]]]

Row = Tuple[bool, List[ElementNode]]
# Width, node, and how the word joins the one before it: "space" (a line may
# break here, and a space is drawn when it does not), "none" (glued to it, as
# in `**bold**;`) or "break" (the tail of a word too long for its column).
Word = Tuple[int, Node, str]
SPACE, GLUED, BREAK = "space", "none", "break"


def _display_width(text: str, ambiguous: int = 1) -> int:
    """Character columns, counting East Asian Ambiguous glyphs as `ambiguous`.

    A table holding CJK is set in a CJK monospace font, which draws the
    ambiguous glyphs — curly quotes, the em dash — full width; the Latin-only
    font used otherwise draws them in one column.
    """
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        kind = unicodedata.east_asian_width(char)
        if kind in WIDE_WIDTHS:
            width += 2
        elif kind == "A":
            width += ambiguous
        else:
            width += 1
    return width


def _longest_unit(text: str, ambiguous: int) -> int:
    """The widest run that cannot be broken across lines.

    Latin words break at spaces only, but a wide glyph may start a line on its
    own, so a run of CJK never reserves more than one character of column.
    """
    longest = 0
    run = ""
    for piece in text.split():
        for char in piece:
            if unicodedata.east_asian_width(char) in WIDE_WIDTHS:
                longest = max(longest, _display_width(run, ambiguous), 2)
                run = ""
            else:
                run += char
        longest = max(longest, _display_width(run, ambiguous))
        run = ""
    return longest


def _is_cjk(char: str) -> bool:
    """A wide glyph from a CJK script, as against a wide emoji.

    Emoji are East Asian Wide too, but they come from a colour font whatever
    the stack says, so they are no reason to set a table in the CJK one. They
    sit below the CJK blocks (⌚, ☕, ⭐) and in the pictograph planes from
    U+1F000; Hangul Jamo is the one CJK block below U+2E80.
    """
    if unicodedata.east_asian_width(char) not in WIDE_WIDTHS:
        return False
    point = ord(char)
    if point < 0x2E80:
        return 0x1100 <= point <= 0x115F
    return not 0x1F000 <= point < 0x20000


def _has_cjk(rows: Sequence[Row]) -> bool:
    return any(
        _is_cjk(char)
        for _, cells in rows
        for cell in cells
        for char in _cell_text(cell)
    )


def _cell_text(cell: ElementNode) -> str:
    pieces: List[str] = []

    def collect(nodes: Sequence[Node]) -> None:
        for child in nodes:
            if isinstance(child, TextNode):
                pieces.append(child.text)
            else:
                collect(child.children)

    collect(cell.children)
    # minihtml collapses runs of whitespace, so measure what it will render.
    return " ".join("".join(pieces).split())


def _rows(table: ElementNode) -> List[Row]:
    rows: List[Row] = []

    def visit(node: ElementNode, in_head: bool) -> None:
        for child in node.children:
            if not isinstance(child, ElementNode):
                continue
            if child.tag == "tr":
                cells = [
                    cell
                    for cell in child.children
                    if isinstance(cell, ElementNode) and cell.tag in CELL_TAGS
                ]
                header = in_head or (
                    bool(cells) and all(cell.tag == "th" for cell in cells)
                )
                rows.append((header, cells))
            elif child.tag in SECTION_TAGS:
                visit(child, in_head or child.tag == "thead")
            elif child.tag != "caption":
                visit(child, in_head)

    visit(table, False)
    return rows


def _caption(table: ElementNode) -> Optional[ElementNode]:
    for child in table.children:
        if isinstance(child, ElementNode) and child.tag == "caption":
            return ElementNode(
                "div",
                {"class": "md-table-caption"},
                list(child.children),
                generated=True,
            )
    return None


def _style(chain: Sequence[ElementNode], header: bool) -> str:
    """The measuring style of text inside `chain`, in a header row or not."""
    tags = {element.tag for element in chain}
    bold = header or bool(tags & {"strong", "b"})
    italic = bool(tags & {"em", "i"})
    return ("b" if bold else "") + ("i" if italic else "") + (
        "c" if "code" in tags else ""
    )


class _Monospace:
    """Character columns, padded with U+00A0: the layout ADR 0007 describes."""

    gap = COLUMN_GAP
    minimum = MIN_COLUMN

    def __init__(self, ambiguous: int) -> None:
        self.ambiguous = ambiguous

    def width(self, text: str, style: str) -> int:
        return _display_width(text, self.ambiguous)

    def space(self) -> int:
        return 1

    def extents(self, cell: ElementNode, header: bool) -> Tuple[int, int]:
        text = _cell_text(cell)
        return (
            _display_width(text, self.ambiguous),
            _longest_unit(text, self.ambiguous),
        )


class _Proportional:
    """Whole pixels, from widths the host measured for each character."""

    def __init__(self, widths: Mapping[str, Mapping[str, float]], font_px: int) -> None:
        self._widths = widths
        self.gap = max(1, round(PROPORTIONAL_GAP_EM * font_px))
        self.minimum = max(1, round(PROPORTIONAL_MIN_EM * font_px))
        self._space = math.ceil(widths[""][" "])

    def width(self, text: str, style: str) -> int:
        table = self._widths[style]
        drawn = sum(table[char] for char in text if not unicodedata.combining(char))
        return math.ceil(drawn * MEASURE_SAFETY) if drawn else 0

    def space(self) -> int:
        return self._space

    def extents(self, cell: ElementNode, header: bool) -> Tuple[int, int]:
        words = _words(cell, 1 << 30, self, header)
        longest = max((sum(word[0] for word in run) for run in _runs(words)), default=0)
        return _line_width(words, self), longest


class _Collector:
    """Stands in for the metrics once, to learn which characters each style
    needs before anything is asked of the measurer."""

    gap = COLUMN_GAP
    minimum = MIN_COLUMN

    def __init__(self) -> None:
        self.needed: Dict[str, Set[str]] = {"": {" "}}

    def width(self, text: str, style: str) -> int:
        self.needed.setdefault(style, set()).update(
            char for char in text if not unicodedata.combining(char)
        )
        return len(text)

    def space(self) -> int:
        return 1


@dataclass(frozen=True)
class ProportionalTables:
    """What a proportional layout needs: the font, its size, the width it has
    to fit and the measurer that knows the host's widths."""

    family: str
    font_px: int
    budget_px: int
    measure: TextMeasurer


def _column_extents(rows: Sequence[Row], metrics) -> Tuple[List[int], List[int]]:
    """Per column: the width that needs no wrapping, and its longest word."""
    natural: List[int] = []
    longest: List[int] = []
    for header, cells in rows:
        for index, cell in enumerate(cells):
            width, word = metrics.extents(cell, header)
            if index < len(natural):
                natural[index] = max(natural[index], width)
                longest[index] = max(longest[index], word)
            else:
                natural.append(width)
                longest.append(word)
    return natural, longest


def _allocate(
    natural: Sequence[int], longest: Sequence[int], budget: int, metrics
) -> List[int]:
    """Share `budget` over the columns.

    Every column keeps room for its longest unbreakable run, so wrapping breaks
    between words rather than inside them; the columns with the most room to
    spare give up the rest, which keeps a narrow column whole while a wide one
    takes the wrapping.
    """
    count = len(natural)
    if not count:
        return []
    minimum = metrics.minimum
    available = budget - metrics.gap * (count - 1)
    if available >= sum(natural):
        return list(natural)
    available = max(available, minimum * count)
    floors = [
        max(minimum, min(longest[index], natural[index])) for index in range(count)
    ]
    if sum(floors) >= available:
        return [max(minimum, width) for width in _share(floors, available)]
    widths = list(natural)
    deficit = sum(widths) - available
    while deficit > 0:
        index = max(
            range(count), key=lambda column: (widths[column] - floors[column], -column)
        )
        if widths[index] <= floors[index]:
            break
        # One unit at a time keeps the shares even; a pixel layout has a
        # great many units, so it gives them up a few at a time.
        step = (
            1
            if isinstance(metrics, _Monospace)
            else min(deficit, widths[index] - floors[index], 8)
        )
        widths[index] -= step
        deficit -= step
    return widths


def _share(weights: Sequence[int], available: int) -> List[int]:
    """Split `available` in proportion to `weights`, losing none."""
    total = sum(weights)
    shares = [0] * len(weights)
    if available <= 0:
        return shares
    remaining = available
    for index in range(len(weights)):
        if index == len(weights) - 1:
            shares[index] = max(0, remaining)
        else:
            shares[index] = weights[index] * available // max(total, 1)
            remaining -= shares[index]
    return shares


def _chunks(text: str, width: int, metrics, style: str) -> List[str]:
    """Hard-split a word too long for its column, so it cannot overflow."""
    if metrics.width(text, style) <= width:
        return [text]
    pieces: List[str] = []
    current = ""
    for char in text:
        # The piece as a whole, not a sum of its characters: a proportional
        # width is rounded up once per measurement, and rounding each
        # character would split a word that fits.
        if current and metrics.width(current + char, style) > width:
            pieces.append(current)
            current = ""
        current += char
    if current:
        pieces.append(current)
    return pieces


def _clone_chain(node: Node, chain: Sequence[ElementNode]) -> Node:
    for element in reversed(chain):
        node = ElementNode(element.tag, dict(element.attrs), [node], generated=True)
    return node


def _words(cell: ElementNode, width: int, metrics, header: bool = False) -> List[Word]:
    """Flatten a cell into wrappable words, each keeping its own markup.

    A word carries a clone of the elements it sits inside, so breaking a line
    inside a link or a bold run reopens that markup on the next line. Whether
    the source had whitespace before a word is carried with it, so a boundary
    such as `**bold**;` neither gains a space nor a line break.
    """
    words: List[Word] = []
    gap = [False]

    def visit(nodes: Sequence[Node], chain: Tuple[ElementNode, ...]) -> None:
        for child in nodes:
            if isinstance(child, TextNode):
                pieces = child.text.split()
                if not pieces:
                    gap[0] = gap[0] or bool(child.text)
                    continue
                style = _style(chain, header)
                leading = gap[0] or child.text[:1].isspace()
                for position, piece in enumerate(pieces):
                    joint = SPACE if position or leading else GLUED
                    for order, chunk in enumerate(_chunks(piece, width, metrics, style)):
                        words.append(
                            (
                                metrics.width(chunk, style),
                                _clone_chain(TextNode(chunk), chain),
                                BREAK if order else joint,
                            )
                        )
                gap[0] = child.text[-1:].isspace()
            elif child.tag in DROPPED_CELL_TAGS:
                continue
            elif child.children:
                visit(child.children, chain + (child,))
            else:
                # Unmeasurable, and kept whole: an image or an empty element.
                words.append(
                    (0, _clone_chain(child, chain), SPACE if gap[0] else GLUED)
                )
                gap[0] = False

    visit(cell.children, ())
    return words


def _runs(words: Sequence[Word]) -> List[List[Word]]:
    """Group words that are glued to their neighbour and so cannot be split."""
    runs: List[List[Word]] = []
    for word in words:
        if runs and word[2] == GLUED:
            runs[-1].append(word)
        else:
            runs.append([word])
    return runs


def _line_width(line: Sequence[Word], metrics) -> int:
    return sum(
        word[0] + (metrics.space() if index and word[2] == SPACE else 0)
        for index, word in enumerate(line)
    )


def _lines(words: Sequence[Word], width: int, metrics) -> List[List[Word]]:
    lines: List[List[Word]] = []
    current: List[Word] = []
    used = 0
    for run in _runs(words):
        size = sum(word[0] for word in run)
        space = metrics.space() if (current and run[0][2] == SPACE) else 0
        if current and used + space + size > width:
            lines.append(current)
            current = [(run[0][0], run[0][1], BREAK)] + list(run[1:])
            used = size
        else:
            current.extend(run)
            used += space + size
    if current:
        lines.append(current)
    return lines or [[]]


_TEXT_ALIGN = re.compile(r"text-align\s*:\s*(left|right|center)", re.I)


def _alignment(cell: ElementNode) -> str:
    # Python-Markdown 3.2 writes `align="right"`; 3.4 and later write
    # `style="text-align: right;"`. Package Control serves both, by host.
    align = cell.attrs.get("align", "")
    if not align:
        matched = _TEXT_ALIGN.search(cell.attrs.get("style", ""))
        align = matched.group(1) if matched else ""
    return align.lower()


def _padding(deficit: int, align: str) -> Tuple[int, int]:
    if deficit <= 0:
        return 0, 0
    if align == "right":
        return deficit, 0
    if align == "center":
        lead = deficit // 2
        return lead, deficit - lead
    return 0, deficit


def _line_children(line: Sequence[Word]) -> List[Node]:
    children: List[Node] = []
    for index, word in enumerate(line):
        if index and word[2] == SPACE:
            children.append(TextNode(" "))
        children.append(word[1])
    return children


def _cell_span(
    line: Sequence[Word], width: int, align: str, last: bool, metrics
) -> ElementNode:
    lead, trail = _padding(width - _line_width(line, metrics), align)
    if last:
        # Nothing follows on the line, so trailing padding would be invisible.
        trail = 0
    else:
        trail += COLUMN_GAP
    children: List[Node] = []
    if lead:
        children.append(TextNode(NBSP * lead))
    children.extend(_line_children(line))
    if trail:
        children.append(TextNode(NBSP * trail))
    return ElementNode("span", {"class": "md-table-cell"}, children, generated=True)


def _spaced_cell(
    line: Sequence[Word], width: int, align: str, last: bool, metrics
) -> ElementNode:
    """A proportional cell: its line in a block the spacer holds `width` wide.

    The block takes the cell's width from the spacer beneath it, so alignment
    is `text-align` inside it rather than padding. The gap to the next column
    is the cell's own right padding, outside the width the text is aligned in.
    """
    block_style = "display: block"
    if align in ("right", "center"):
        block_style += "; text-align: {}".format(align)
    text = _line_children(line) or [TextNode(NBSP)]
    spacer = ElementNode(
        "img",
        {
            "class": "md-table-spacer",
            "src": SPACER,
            "width": str(width),
            "height": "1",
            "style": "width: {}px; height: 1px".format(width),
        },
        [],
        generated=True,
    )
    cell_style = "display: inline-block"
    if not last:
        cell_style += "; padding-right: {}px".format(metrics.gap)
    return ElementNode(
        "span",
        {"class": "md-table-cell", "style": cell_style},
        [ElementNode("span", {"style": block_style}, text, generated=True), spacer],
        generated=True,
    )


def _row_nodes(row: Row, widths: Sequence[int], metrics) -> List[ElementNode]:
    header, cells = row
    wrapped = [
        _lines(_words(cell, widths[index], metrics, header), widths[index], metrics)
        for index, cell in enumerate(cells)
    ]
    height = max((len(lines) for lines in wrapped), default=1)
    classes = "md-table-row md-table-head" if header else "md-table-row"
    emit = _spaced_cell if isinstance(metrics, _Proportional) else _cell_span
    nodes: List[ElementNode] = []
    for line in range(height):
        spans: List[Node] = []
        for index, lines in enumerate(wrapped):
            spans.append(
                emit(
                    lines[line] if line < len(lines) else [],
                    widths[index],
                    _alignment(cells[index]),
                    index == len(cells) - 1,
                    metrics,
                )
            )
        nodes.append(ElementNode("div", {"class": classes}, spans, generated=True))
    return nodes


def table_font_px(rem_px: int, scale: float) -> int:
    """The table's font size in whole pixels.

    Snapped to a pixel so that the advance `budgets` rounds is the one minihtml
    draws: the rounding was measured at a whole-pixel size, and a fractional
    one would leave it to the host which way 13.6 px goes.
    """
    return max(1, round(rem_px * scale))


def table_style(rem_px: int, scale: float) -> str:
    """The inline style a scaled table carries, or "" at the stylesheet's size."""
    font_px = table_font_px(rem_px, scale)
    if font_px == rem_px or rem_px <= 0:
        return ""
    ratio = font_px / rem_px
    return "font-size: {:.4f}rem; line-height: {:.4f}rem".format(
        ratio, LINE_REM * ratio
    )


def budgets(
    viewport_width: float, rem_px: int, cap: int, scale: float = 1.0
) -> Tuple[int, int]:
    """Characters that fit the preview, for the Latin and the CJK font.

    The two fonts have different advances, and which one a table uses is only
    known once its content has been read, so both are worked out here and the
    table picks one. Where the CJK font is not shipped, both are the Latin one.
    `scale` is `table_font_scale`: a smaller table font fits more characters
    into the same width.
    """
    fallback = min(cap, FALLBACK_COLUMNS)
    if viewport_width <= 0 or rem_px <= 0:
        return fallback, fallback
    content = viewport_width - CHROME_REM * rem_px
    cjk_advance = CJK_ADVANCE if CJK_FONT_SHIPS else LATIN_ADVANCE
    font_px = table_font_px(rem_px, scale)
    return tuple(  # type: ignore[return-value]
        max(
            MIN_COLUMN,
            min(cap, int(content // math.ceil(advance * font_px)) - SAFETY_COLUMNS),
        )
        for advance in (LATIN_ADVANCE, cjk_advance)
    )


def pixel_budget(
    viewport_width: float, rem_px: int, cap: int, scale: float = 1.0
) -> int:
    """The width a proportional table may take, in pixels.

    The same chrome as `budgets`, less one rem for the scrollbar and the host's
    rounding; `table_max_columns` still bounds it on a very wide screen, at an
    average character's width.
    """
    font_px = table_font_px(rem_px, scale)
    widest = int(cap * AVERAGE_EM * font_px)
    if viewport_width <= 0 or rem_px <= 0:
        return min(widest, int(FALLBACK_COLUMNS * AVERAGE_EM * font_px))
    content = viewport_width - (CHROME_REM + 1) * rem_px
    return max(1, min(widest, int(content)))


def _metrics_for(
    tables: Sequence[ElementNode], proportional: ProportionalTables
) -> Optional[_Proportional]:
    """Measured metrics for every character these tables use, or None while
    the measurer has yet to learn some of them."""
    collector = _Collector()
    for table in tables:
        for header, cells in _rows(table):
            for cell in cells:
                _words(cell, 1 << 30, collector, header)
    widths: Dict[str, Mapping[str, float]] = {}
    for style, chars in sorted(collector.needed.items()):
        found = proportional.measure(
            proportional.family, proportional.font_px, style, frozenset(chars)
        )
        if found is None:
            return None
        widths[style] = found
    return _Proportional(widths, proportional.font_px)


def _table_node(
    table: ElementNode,
    latin: int,
    cjk: int,
    style: str = "",
    proportional: Optional[ProportionalTables] = None,
    measured: Optional[_Proportional] = None,
) -> ElementNode:
    rows = _rows(table)
    if measured is not None and proportional is not None:
        metrics = measured
        budget = proportional.budget_px
        wide = False
        # Double quotes only: the serialiser writes them as `&quot;`, which
        # minihtml reads; a family in single quotes is dropped.
        style = "; ".join(
            part
            for part in (style, "font-family: {}".format(proportional.family))
            if part
        )
    else:
        wide = _has_cjk(rows)
        metrics = _Monospace(2 if wide else 1)
        budget = cjk if wide else latin
    natural, longest = _column_extents(rows, metrics)
    widths = _allocate(natural, longest, budget, metrics)
    children: List[Node] = []
    caption = _caption(table)
    if caption is not None:
        children.append(caption)
    for index, row in enumerate(rows):
        nodes = _row_nodes(row, widths, metrics)
        last_head = row[0] and (index + 1 == len(rows) or not rows[index + 1][0])
        if last_head and nodes:
            nodes[-1].attrs["class"] += " md-table-rule"
        children.extend(nodes)
    classes = "md-table md-table-cjk" if wide else "md-table"
    attrs = {"class": classes}
    if style:
        attrs["style"] = style
    return ElementNode("div", attrs, children, generated=True)


def _tables(nodes: List[Node]) -> Iterator[Tuple[List[Node], int]]:
    """Where each outermost ``table`` sits: its siblings and its index."""
    for index, node in enumerate(nodes):
        if not isinstance(node, ElementNode):
            continue
        if node.tag == "table":
            yield nodes, index
            continue
        yield from _tables(node.children)


def replace_tables(
    nodes: List[Node],
    latin: int,
    cjk: int,
    style: str = "",
    proportional: Optional[ProportionalTables] = None,
) -> None:
    """Replace every ``table`` subtree in place with aligned rows.

    `style` comes from `table_style`, and sizes the table's font to match the
    budget it was laid out for. With `proportional`, the tables are set in its
    font once every character they use has been measured, and in monospace
    until then (ADR 0024).
    """
    places = list(_tables(nodes))
    measured = None
    if proportional is not None and places:
        measured = _metrics_for(
            [siblings[index] for siblings, index in places], proportional
        )
    for siblings, index in places:
        siblings[index] = _table_node(
            siblings[index], latin, cjk, style, proportional, measured
        )
