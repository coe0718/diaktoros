"""Constants shared by the host broker and the in-sandbox client.

The client is copied into the sandbox on its own (it cannot import the host modules), so anything
both sides must agree on lives here, in a module with no imports, and is copied in alongside it.
"""

# Opens the host-written header of a fixer-answers PR comment. The client refuses answers that
# contain it (so a seat cannot forge a second record), and the host writes and parses it.
ANSWERS_MARKER = "<!-- diaktoros:fixer-answers"
# Every marker a record may carry: comments posted before the rename (#425) keep the old one, and
# are still records. The host writes only the new one; a seat's text may contain neither.
ANSWERS_MARKERS = (ANSWERS_MARKER, "<!-- review-loop:fixer-answers")
