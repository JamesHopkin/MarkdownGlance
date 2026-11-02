# 24. Tables can be set in a proportional font

Date: 2026-11-02

## Status

Proposed. Extends [ADR 0007](0007-table-rendering-under-minihtml.md), whose
monospace layout stays the default.

## Context

ADR 0007 set tables in monospace because minihtml has no `<table>` and no
`width`, so a column can only be lined up by padding characters of known
width. A monospace table takes more room than the same text in the body font,
and wraps sooner.

Probes in Sublime Text 4 on macOS found a way through:

- **A cell can be held at a width.** An inline-block holding the cell's text
  and, beneath it, a 1×1 transparent image stretched with inline `width`
  is as wide as the image, whatever its text, so long as the text is not wider.
  Columns of such cells line up in any font.
- **The host can say how wide it draws text.** An inline phantom pushes the
  text after it right by its own width, which `View.text_to_layout()` reports.
  One phantom per line, ten copies of a character and twenty, gives that
  character's advance with the phantom's own chrome cancelled. 880 such
  readings take about 0.16 s and match one-at-a-time readings exactly.
- **Only in a view on screen.** A hidden output panel answers zero for every
  phantom, straight away or a second later, so the highlighter's hidden-panel
  approach (ADR 0021) cannot be used.
- **A font named inline must use double quotes.** `font-family: "Arial
  Narrow"`, written by the serialiser as `&quot;`, is honoured; single quotes
  fall back to the default serif, and a `<style>` inside the body is ignored.

## Decision

**`table_font_family` opts in.** Empty, the default, is ADR 0007's layout,
unchanged. A font list, checked and quoted as `font_family` is (ADR 0022),
sets every table in that font.

**One layout, two metrics.** The allocator, wrapping and word handling are
ADR 0007's, made to take their widths from a metrics object. The monospace
one counts characters as before; the proportional one sums measured advances
per style -- regular, bold, italic, code -- in whole pixels with a 5% margin
for kerning, which a spacer turns from overflow into a little slack. A cell is
emitted as an inline-block with its line in a block, aligned by `text-align`,
over a spacer of the column's width; the gap to the next column is its right
padding.

**Measured in the preview, below the document.** The `TextMeasurer` a render
is given answers from its cache, or queues what it lacks and returns nothing,
and that render sets its tables in monospace. On the UI thread the queue is
measured in a preview that is on screen: lines of spaces appended below the
document, one inline phantom of transparent text on each, read on the next
tick and cut away again. The first request for a font, size and style takes
all of printable ASCII with it, so a document usually needs one pass. Then the
documents on screen are rendered again.

**The poll watches pixels too.** With a family set, the resize poll compares
the pixel budget as well as the character ones.

## Consequences

- Tables can be set in the body font or a narrow one: Arial Narrow fits about
  40% more text per line than the monospace font at the same size.
- The first render with a new font, size or zoom shows monospace for a frame
  or two. A preview in a background tab cannot be measured; it asks again on
  its next render.
- The preview's buffer is edited for those two ticks. Its size and layout
  extent grow while the lines are there, and anything reading them in that
  window sees the larger figure.
- A character the cache has not seen is measured on demand, so CJK and emoji
  are measured rather than assumed; a cell's image still counts as no width.
- Each line of each cell carries a spacer image: a few hundred bytes of HTML
  per cell line.
