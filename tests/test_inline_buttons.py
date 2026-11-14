"""Message structure policy: no vocabulary, join requirement, or AI dependency."""

import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.archive import snapshot
from purrai.message_buttons import has_inline_buttons
from purrai.models import ArchivedMessage, Member, MessageRevision, Violation, Whitelist


async def enable(client, **settings):
    response = await client.patch('/api/groups/-1001/settings', json={
        'settings': {'inline_buttons_ban': True, **settings},
    })
    assert response.status_code == 200, response.text


def button_message(identifier=301, *, source='guest', button=None):
    incoming = message(identifier, '1', message_thread_id=77,
        reply_markup={'inline_keyboard': [[button or {'text': '纸飞机', 'url': 'https://example.test'}]]})
    incoming['reply_to_message'] = message(77, '频道帖子', sender_chat={'id': -2001, 'type': 'channel'},
                                            is_automatic_forward=True)
    if source == 'guest':
        incoming['guest_bot_caller_user'] = {'id': 42, 'is_bot': False, 'first_name': '成员'}
        incoming['from'] = {'id': 7001, 'is_bot': True, 'first_name': 'Guest helper'}
    elif source == 'inline':
        incoming['via_bot'] = {'id': 7001, 'is_bot': True, 'first_name': 'Inline helper'}
    return incoming


@pytest.mark.parametrize('source,button', [
    ('direct', {'text': '纸飞机', 'url': 'https://example.test'}),
    ('inline', {'text': '南瓜', 'callback_data': 'arbitrary'}),
    ('guest', {'text': '随机按钮', 'copy_text': {'text': 'anything'}}),
])
async def test_buttons_ban_sender_without_join_record_and_archive_evidence(env, client, source, button):
    _, db, tg, svc = env
    await enable(client)
    async with db.sessions() as session:
        assert await session.get(Member, (-1001, 42)) is None
    tg.members[(-1001, 42)] = {'status': 'left'}
    incoming = button_message(source=source, button=button)
    await svc.message(incoming, update_id=301)
    await drain(svc)
    assert [kw['user_id'] for method, kw in tg.calls if method == 'ban_chat_member'] == [42]
    assert [kw['message_id'] for method, kw in tg.calls if method == 'delete_message'] == [301]
    assert tg.members[(-1001, 42)] == {'status': 'kicked', 'until_date': 0}
    assert not any(method == 'restrict_chat_member' for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.scalar(select(Violation))).user_id == 42
        revision = await session.scalar(select(MessageRevision))
        assert revision.payload['reply_markup'] == incoming['reply_markup']
        assert (await session.scalar(select(ArchivedMessage))).deletion_state == 'deleted'
    # Replay cannot produce a second permanent ban or deletion.
    await svc.message(incoming, update_id=301)
    await drain(svc)
    assert sum(method == 'ban_chat_member' for method, _ in tg.calls) == 1


async def test_button_setting_and_preview_are_managed_data(env, client):
    preview_url = '/api/groups/-1001/rules/preview'
    before = (await client.post(preview_url, json={'text': '1', 'inline_buttons': True})).json()
    assert not before['delete_message'] and not before['ban_member']
    await enable(client)
    enabled = (await client.post(preview_url, json={'text': '1', 'inline_buttons': True})).json()
    assert enabled['delete_message'] and enabled['ban_member'] and not enabled['mute_member']
    await enable(client, inline_buttons_ban=False)
    disabled = (await client.post(preview_url, json={'text': '1', 'inline_buttons': True})).json()
    assert not disabled['delete_message'] and not disabled['ban_member']
    await env[3].message(button_message())
    await drain(env[3])
    assert not any(method in ('ban_chat_member', 'delete_message') for method, _ in env[2].calls)


@pytest.mark.parametrize('exemption', ['administrator', 'whitelist', 'owner', 'own_bot', 'automatic_forward'])
async def test_buttons_keep_existing_exemptions(env, client, exemption):
    _, db, tg, svc = env
    await enable(client)
    incoming = button_message()
    if exemption == 'administrator':
        tg.members[(-1001, 42)] = {'status': 'administrator'}
    elif exemption == 'whitelist':
        async with db.transaction() as session:
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
    elif exemption == 'owner':
        incoming['guest_bot_caller_user']['id'] = 1
    elif exemption == 'own_bot':
        incoming['from']['id'] = tg.user_id
    else:
        incoming.update(sender_chat={'id': -2001, 'type': 'channel'}, is_automatic_forward=True)
    await svc.message(incoming)
    await drain(svc)
    assert not any(method in ('ban_chat_member', 'delete_message', 'restrict_chat_member') for method, _ in tg.calls)


@pytest.mark.parametrize('archive_mode', ['on', 'off', 'off_after_receipt'])
async def test_removing_buttons_before_execution_cancels_both_actions(env, client, archive_mode):
    _, db, tg, svc = env
    await enable(client, archive_enabled=archive_mode != 'off')
    incoming = button_message()
    await svc.message(incoming, update_id=301)
    if archive_mode == 'off_after_receipt':
        await enable(client, archive_enabled=False)
    edited = {**incoming, 'reply_markup': {'inline_keyboard': []}, 'edit_date': incoming['date'] + 1}
    await svc.message(edited, edited=True, update_id=302)
    await drain(svc)
    assert not any(method in ('ban_chat_member', 'delete_message') for method, _ in tg.calls)
    async with db.sessions() as session:
        member = await session.get(Member, (-1001, 42))
        assert not member.owned_restriction


async def test_disabling_button_policy_before_execution_cancels_both_actions(env, client):
    _, _, tg, svc = env
    await enable(client)
    await svc.message(button_message())
    await enable(client, inline_buttons_ban=False)
    await drain(svc)
    assert not any(method in ('ban_chat_member', 'delete_message') for method, _ in tg.calls)


async def test_setting_change_during_member_lookup_does_not_claim_ban_ownership(env, client):
    _, db, tg, svc = env
    await enable(client)
    await svc.message(button_message())
    original_call = tg.call
    changed = False
    async def change_at_lookup(method, **kwargs):
        nonlocal changed
        result = await original_call(method, **kwargs)
        if method == 'get_chat_member' and not changed:
            changed = True
            await enable(client, inline_buttons_ban=False)
        return result
    tg.call = change_at_lookup
    await drain(svc)
    assert changed
    assert not any(method in ('ban_chat_member', 'delete_message') for method, _ in tg.calls)
    async with db.sessions() as session:
        assert not (await session.get(Member, (-1001, 42))).owned_restriction


async def test_button_added_by_edit_is_detected_without_new_membership(env, client):
    _, _, tg, svc = env
    await enable(client)
    plain = message(301, '1')
    await svc.message(plain, update_id=301)
    await drain(svc)
    assert not any(method == 'ban_chat_member' for method, _ in tg.calls)
    incoming = button_message(source='direct')
    incoming['edit_date'] = plain['date'] + 1
    await svc.message(incoming, edited=True, update_id=302)
    await drain(svc)
    assert tg.members[(-1001, 42)]['status'] == 'kicked'


@pytest.mark.parametrize('markup', [None, {}, {'inline_keyboard': []}, {'inline_keyboard': [[]]},
                                   {'inline_keyboard': [[{}]]}, {'remove_keyboard': True}])
def test_only_actual_buttons_on_this_message_count(markup):
    incoming = message(301, '1', reply_markup=markup, reply_to_message=button_message())
    assert not has_inline_buttons(incoming)
    assert not has_inline_buttons(snapshot(incoming))


async def test_reply_to_buttons_does_not_punish_the_replying_user(env, client):
    _, _, tg, svc = env
    await enable(client)
    await svc.message(message(302, '普通回复', reply_to_message=button_message()))
    await drain(svc)
    assert not any(method in ('ban_chat_member', 'delete_message') for method, _ in tg.calls)
