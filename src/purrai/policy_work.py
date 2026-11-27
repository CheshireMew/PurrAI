"""Checked, short database stages around pure policy computation."""

import asyncio
import copy

from sqlalchemy import inspect, select, func, literal, tuple_, union_all

from purrai.action_execution import ActionExecution, ActionSuperseded
from purrai.models import ArchivedMessage, Group, MessageRevision, Meta
from purrai.message_eligibility import blocked_message
from purrai.shared_policy import effective_rules


class PolicyExecution(ActionExecution):
    async def refresh_rows(self, rows):
        grouped, deleted = {}, set()
        for row in rows:
            grouped.setdefault(type(row), []).append(row)
        for model, records in grouped.items():
            columns = inspect(model).primary_key
            for start in range(0, len(records), 200):
                chunk = records[start:start + 200]
                keys = [inspect(row).identity for row in chunk]
                condition = (columns[0].in_([key[0] for key in keys]) if len(columns) == 1
                             else tuple_(*columns).in_(keys))
                fresh = await self.session.scalars(select(model).where(condition)
                    .execution_options(populate_existing=True))
                present = {inspect(row).identity for row in fresh}
                for row, key in zip(chunk, keys):
                    if key not in present:
                        deleted.add(id(row))
                        if row in self.session:
                            self.session.expunge(row)
        return deleted

    @staticmethod
    def identity(row):
        # These stages only prepare conversation/review decisions. Unlike an
        # already-sent member restriction, every source/configuration change
        # must restart a still-uncommitted policy decision.
        return tuple(copy.deepcopy(getattr(row, column.key)) for column in inspect(type(row)).columns)


async def source_signature(session):
    result = []
    grouped = {}
    for (group_id, message_id), sender_id in session.info.get("policy_messages", {}).items():
        grouped.setdefault(group_id, {})[message_id] = sender_id
    for group_id, identifiers in sorted(grouped.items()):
        blocked, sources = [], []
        items = list(identifiers.items())
        # Preparing several topics can accumulate more than SQLite's 500
        # compound-SELECT terms. Bound both this query and its IN parameters.
        for start in range(0, len(items), 200):
            chunk = dict(items[start:start + 200])
            candidates = union_all(*(select(literal(identifier).label("message_id"), literal(sender).label("sender_id"))
                for identifier, sender in chunk.items())).subquery()
            blocked.extend(await session.scalars(select(candidates.c.message_id).where(
                blocked_message(group_id, candidates.c.message_id, candidates.c.sender_id))))
            rows = await session.execute(select(ArchivedMessage.message_id, ArchivedMessage.text, ArchivedMessage.caption,
                ArchivedMessage.sender_id, ArchivedMessage.sender_name, ArchivedMessage.username,
                ArchivedMessage.edited_at, ArchivedMessage.deletion_state, func.max(MessageRevision.id))
                .outerjoin(MessageRevision, MessageRevision.archive_id == ArchivedMessage.id)
                .where(ArchivedMessage.group_id == group_id, ArchivedMessage.message_id.in_(chunk))
                .group_by(ArchivedMessage.id))
            sources.extend(tuple(row) for row in rows)
        result.append((group_id, tuple(sorted(blocked)), tuple(sorted(sources, key=lambda row: row[0]))))
    groups = {row.id for row in session.identity_map.values() if isinstance(row, Group)}
    for group_id in sorted(groups):
        keys = (f"chat_reset:{group_id}", f"chat_input:{group_id}", f"chat_participation:{group_id}")
        values = dict((await session.execute(select(Meta.key, Meta.value).where(Meta.key.in_(keys)))).all())
        result.extend((key, values.get(key)) for key in keys)
    return result


async def policy_work(session, function, *args, **kwargs):
    execution = session.info.get("action_execution")
    if execution is None:
        if session.info.get("database_writer"):
            raise RuntimeError("Policy computation requires a checked handoff outside the writer")
        return await asyncio.to_thread(function, *args, **kwargs)
    groups = [row.id for row in session.identity_map.values() if isinstance(row, Group)]
    # A newly added local rule has no pre-existing ORM object to refresh.
    # Recheck membership as well as the existing policy objects after the I/O.
    before = {identifier: copy.deepcopy(await effective_rules(session, identifier)) for identifier in groups}
    snapshots = [] if isinstance(execution, PolicyExecution) else [
        (row, PolicyExecution.identity(row)) for row in session.identity_map.values()]
    sources = await source_signature(session)
    result = await execution.external(lambda: asyncio.to_thread(function, *args, **kwargs))
    if any(row not in session or PolicyExecution.identity(row) != identity for row, identity in snapshots):
        raise ActionSuperseded(reason="计算期间配置或任务状态已变化，将重新检查", before_request=True)
    for identifier, rules in before.items():
        if rules != await effective_rules(session, identifier):
            raise ActionSuperseded(reason="审核配置已变化，将重新检查", before_request=True)
    if sources != await source_signature(session):
        raise ActionSuperseded(reason="消息来源或聊天状态已变化，将重新检查", before_request=True)
    return result
