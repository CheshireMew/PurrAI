import io
import time

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from conftest import drain, join, message
from purrai.identity_protection import config_for, matching_name, same_photo
from purrai.models import Action, Member, Whitelist
from purrai.schemas import GroupSettings


async def configure(client, **values):
    response = await client.patch('/api/groups/-1001/settings', json={'settings': values})
    assert response.status_code == 200, response.text


async def setup(env, client, *, bio='任意测试简介', photo=None, name='任意测试名称（附加说明）'):
    _, db, tg, service = env
    await configure(client, identity_enabled=True, identity_user_id=123, identity_names=['任意测试名称'])
    service.identity_protection.interval = 0
    user = {'id': 42, 'first_name': name, 'is_bot': False}
    tg.members[-1001, 42] = {'status': 'member', 'user': user}
    original = tg.call

    async def call(method, **kwargs):
        if method == 'get_chat' and kwargs['chat_id'] > 0:
            tg.calls.append((method, kwargs))
            return {'id': kwargs['chat_id'], 'type': 'private', 'bio': bio}
        if method == 'get_user_profile_photos':
            tg.calls.append((method, kwargs))
            return {'photos': [[photo]] if photo else [], 'total_count': 1 if photo else 0}
        return await original(method, **kwargs)
    tg.call = call
    return db, tg, service, user


async def action_row(db):
    async with db.sessions() as session:
        return await session.scalar(select(Action).where(Action.kind == 'identity_check'))


def test_configured_names_parentheses_and_nonmatching_prefixes():
    cfg = config_for(GroupSettings(identity_names=['测试甲']).model_dump())
    assert matching_name(' 测试甲（任何后缀）', cfg)
    assert matching_name('测\u200b试甲', cfg)
    assert not matching_name('测试甲乙', cfg)
    cfg['identity_parentheses'] = False
    assert not matching_name('测试甲（任何后缀）', cfg)


async def test_join_same_bio_kicks_and_records_evidence(env, client):
    db, tg, service, user = await setup(env, client)
    await configure(client, verification_enabled=False)
    incoming = join()
    incoming['new_chat_member']['user'] = user
    await service.member_event(incoming)
    await drain(service)
    methods = [m for m, _ in tg.calls]
    assert methods.count('ban_chat_member') == 1 and methods.count('unban_chat_member') == 1
    row = await action_row(db)
    assert row.state == 'succeeded' and row.payload['identity_evidence']['matched'] == ['bio']
    assert tg.members[-1001, 42]['status'] == 'left'


async def test_message_checks_photo_and_keeps_real_account(env, client):
    photo = {'file_unique_id': 'same-picture', 'file_id': 'file', 'width': 640, 'height': 640}
    db, tg, service, user = await setup(env, client, bio='', photo=photo)
    await configure(client, identity_action='ban')
    await service.identity_protection.queue(-1001, {**user, 'id': 123})
    assert await action_row(db) is None
    await service.message(message(1, '普通消息内容', **{'from': user}))
    await drain(service)
    assert tg.members[-1001, 42]['status'] == 'kicked'
    assert not any(m == 'unban_chat_member' for m, _ in tg.calls)
    assert (await action_row(db)).payload['identity_evidence']['matched'] == ['photo']


async def test_empty_profiles_are_not_evidence_and_repeat_is_cached(env, client):
    db, tg, service, user = await setup(env, client, bio='')
    await service.identity_protection.queue(-1001, user)
    await drain(service)
    assert not any(m == 'ban_chat_member' for m, _ in tg.calls)
    assert (await action_row(db)).payload['identity_evidence']['outcome'] == 'not_confirmed'
    before = len(tg.calls)
    await service.identity_protection.queue(-1001, user)
    await drain(service)
    assert len(tg.calls) == before


async def test_names_edit_disable_through_management_and_stale_work(env, client):
    db, tg, service, user = await setup(env, client)
    await configure(client, identity_names=['另一个任意昵称'])
    await service.identity_protection.queue(-1001, user)
    assert await action_row(db) is None
    changed = {**user, 'first_name': '另一个任意昵称'}
    tg.members[-1001, 42]['user'] = changed
    await service.identity_protection.queue(-1001, changed)
    assert (await action_row(db)).state == 'pending'
    await configure(client, identity_enabled=False)
    await drain(service)
    assert (await action_row(db)).state == 'cancelled'
    assert not any(m == 'ban_chat_member' for m, _ in tg.calls)
    await configure(client, identity_enabled=True)
    async with db.transaction() as session:
        row = await session.get(Action, (await action_row(db)).id)
        row.updated_at = time.time() - 601
    await service.identity_protection.queue(-1001, changed)
    await drain(service)
    assert tg.members[-1001, 42]['status'] == 'left'


@pytest.mark.parametrize('exemption', ['admin', 'whitelist'])
async def test_existing_exemptions_remain_effective(env, client, exemption):
    db, tg, service, user = await setup(env, client)
    if exemption == 'admin':
        tg.members[-1001, 42]['status'] = 'administrator'
    else:
        async with db.transaction() as session:
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True, verification=False))
    await service.identity_protection.queue(-1001, user)
    await drain(service)
    assert not any(m == 'ban_chat_member' for m, _ in tg.calls)


async def test_policy_change_during_final_member_lookup_cancels_kick(env, client):
    db, tg, service, user = await setup(env, client)
    original, checks = tg.call, 0
    async def call(method, **kwargs):
        nonlocal checks
        if method == 'get_chat_member' and kwargs.get('user_id') == 42:
            checks += 1
            if checks == 2:
                await configure(client, identity_names=['已经换掉的昵称'])
        return await original(method, **kwargs)
    tg.call = call
    await service.identity_protection.queue(-1001, user)
    await drain(service)
    assert not any(m == 'ban_chat_member' for m, _ in tg.calls)
    assert (await action_row(db)).state == 'cancelled'


async def test_failed_unban_retries_only_owned_kick(env, client):
    db, tg, service, user = await setup(env, client)
    tg.fail['unban_chat_member'] = 1
    await service.identity_protection.queue(-1001, user)
    await service.run_one_action(include_progress=False, include_cleanup=False)
    assert tg.members[-1001, 42]['status'] == 'kicked'
    row = await action_row(db)
    async with db.transaction() as session:
        row = await session.get(Action, row.id)
        row.available_at = 0
    await service.run_one_action(include_progress=False, include_cleanup=False)
    assert tg.members[-1001, 42]['status'] == 'left'
    assert sum(m == 'ban_chat_member' for m, _ in tg.calls) == 1


async def test_confirmed_impostor_can_be_removed_despite_prior_external_mute(env, client):
    db, tg, service, user = await setup(env, client)
    tg.members[-1001, 42] = {'status': 'restricted', 'is_member': True, 'until_date': 0,
                            'can_send_messages': False, 'user': user}
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, present=True, restriction_conflict=True))
    await service.identity_protection.queue(-1001, user)
    await drain(service)
    assert (await action_row(db)).state == 'succeeded'
    assert tg.members[-1001, 42]['status'] == 'left'


async def test_network_failure_never_punishes(env, client):
    db, tg, service, user = await setup(env, client)
    tg.fail['get_chat_member'] = 1
    await service.identity_protection.queue(-1001, user)
    await service.run_one_action(include_progress=False, include_cleanup=False)
    assert (await action_row(db)).state == 'pending'
    assert not any(m == 'ban_chat_member' for m, _ in tg.calls)


async def test_invalid_identity_edits_return_validation_and_preserve_settings(env, client):
    await setup(env, client)
    for changes in ({'identity_user_id': 0}, {'identity_names': []},
                    {'identity_match_bio': False, 'identity_match_photo': False}):
        response = await client.patch('/api/groups/-1001/settings', json={'settings': changes})
        assert response.status_code == 422
    response = await client.patch('/api/groups/-1001/settings', json={'reset': ['identity_user_id']})
    assert response.status_code == 422
    response = await client.patch('/api/groups/-1001/settings', json={
        'settings': {'identity_enabled': False}, 'reset': ['identity_user_id', 'identity_names']})
    assert response.status_code == 200
    assert response.json()['settings']['identity_user_id'] == 0


async def test_telegram_rate_limit_is_respected_without_punishment(env, client):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import GetChatMember
    _, tg, service, user = await setup(env, client)
    original = tg.call
    async def call(method, **kwargs):
        if method == 'get_chat_member' and kwargs.get('user_id') == 42:
            raise TelegramRetryAfter(method=GetChatMember(chat_id=-1001, user_id=42), message='wait', retry_after=37)
        return await original(method, **kwargs)
    tg.call = call
    await service.identity_protection.queue(-1001, user)
    await service.run_one_action(include_progress=False, include_cleanup=False)
    row = await action_row(env[1])
    assert row.state == 'pending' and row.available_at >= time.time() + 35
    assert service.identity_protection.next_request >= time.monotonic() + 36
    assert not any(m == 'ban_chat_member' for m, _ in tg.calls)


def test_reuploaded_compressed_photo_and_distinct_image():
    image = Image.new('RGB', (256, 256), 'white')
    draw = ImageDraw.Draw(image)
    draw.ellipse((30, 30, 220, 220), fill='#ad7e40')
    draw.rectangle((80, 90, 160, 180), fill='#303c77')
    a, b, c = io.BytesIO(), io.BytesIO(), io.BytesIO()
    image.save(a, 'PNG')
    image.resize((180, 180)).save(b, 'JPEG', quality=85)
    Image.new('RGB', (256, 256), 'white').save(c, 'PNG')
    assert same_photo(a.getvalue(), b.getvalue())
    assert not same_photo(a.getvalue(), c.getvalue())
