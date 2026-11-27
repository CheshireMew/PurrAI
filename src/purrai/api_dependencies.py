"""HTTP parameter contracts shared by otherwise independent API adapters."""

from typing import Annotated

from fastapi import Header

RequestKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)]
