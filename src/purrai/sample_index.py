"""Character postings for exact-recall near-copy candidates, without rule vocabulary."""

from collections import Counter

from sqlalchemy import delete, event

from purrai.advertisement_similarity import features


def postings(identifier, group_id, text):
    return [{"sample_id": identifier, "group_id": group_id, "character": char, "occurrences": count}
            for char, count in Counter(features(text)[0]).items()]


def install_index(sample, token):
    @event.listens_for(sample, "after_insert")
    @event.listens_for(sample, "after_update")
    def update_index(mapper, connection, row):
        connection.execute(delete(token).where(token.c.sample_id == row.id))
        values = postings(row.id, row.group_id, row.text)
        if values:
            connection.execute(token.insert(), values)
