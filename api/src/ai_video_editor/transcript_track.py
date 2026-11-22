"""Which transcript track a reader sees.

`transcripts.speaker` is NULL for the mixed (default) mix and 'owner' for the
separately transcribed mic track. Owner speech is already present in the mixed
rows, so every reader that does not explicitly ask for the owner track must
filter to the mixed one -- otherwise the owner is duplicated (and, for RAG,
embedded twice).
"""

MIXED_TRACK_SQL = "speaker IS NULL"
