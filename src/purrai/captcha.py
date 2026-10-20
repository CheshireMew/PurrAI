import hashlib
import io
import secrets
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def digest(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def make_image() -> tuple[str, bytes]:
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    image = Image.new("RGB", (360, 130), "#f2f5fa")
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        draw.line(
            [(secrets.randbelow(360), secrets.randbelow(130)) for _ in range(3)],
            fill=(150 + secrets.randbelow(70), 170, 190),
            width=2,
        )
    font = ImageFont.truetype(str(Path(__file__).parent / "assets" / "NotoSans.ttf"), 53)
    for index, digit in enumerate(code):
        layer = Image.new("RGBA", (64, 90))
        ImageDraw.Draw(layer).text((9, 0), digit, font=font, fill=(35, 55 + secrets.randbelow(70), 100, 255))
        layer = layer.rotate(secrets.randbelow(25) - 12, resample=Image.Resampling.BICUBIC)
        image.paste(layer, (12 + index * 55, 17 + secrets.randbelow(12)), layer)
    image = image.filter(ImageFilter.SMOOTH)
    output = io.BytesIO()
    image.save(output, "PNG")
    return code, output.getvalue()


def make_choices(code: str) -> list[str]:
    choices = {code}
    while len(choices) < 6:
        choices.add(f"{secrets.randbelow(1_000_000):06d}")
    choices = list(choices)
    secrets.SystemRandom().shuffle(choices)
    return choices


def keyboard(verification_id: str, nonce: str, choices=()) -> dict:
    def button(text: str, key: str):
        return {"text": text, "callback_data": f"v:{verification_id}:{nonce}:{key}"}

    if choices:
        buttons = [button(code, f"c{index}") for index, code in enumerate(choices)]
        return {"inline_keyboard": [buttons[start:start + 2] for start in range(0, len(buttons), 2)]}

    # Keep already-issued digit keyboards working until their verification ends.
    return {
        "inline_keyboard": [[button(str(n), str(n)) for n in range(start, start + 3)] for start in (1, 4, 7)]
        + [[button("退格", "back"), button("0", "0"), button("提交", "ok")]]
    }


def caption(
    name: str,
    digits: str,
    attempts: int,
    maximum: int,
    deadline: float,
    join_request=False,
    group_title="",
    failure_action="permanent_mute",
    multiple_choice=True,
) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    time_text = datetime.fromtimestamp(deadline, ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S")
    return (f"入群申请 · {group_title[:80]}\n" if join_request else "") + (
        (
            f"{name[:60]}，欢迎加入！请点击下方与图片中 6 位数字完全相同的答案。\n"
            "只需选择一次，无需逐位输入或再次提交。图片看不清时可点击放大。\n"
            if multiple_choice
            else f"{name[:60]}，欢迎加入！请用下方键盘输入图片中的 6 位数字。\n"
            f"已输入：{'●' * len(digits)}{'○' * (6 - len(digits))}\n"
            "请等按钮回应后继续输入，满 6 位后点提交。\n"
        )
        + f"剩余作答次数：{maximum - attempts} · 截止 {time_text}（北京时间）\n"
        + (
            "完成验证后批准入群。只有你本人能够操作。"
            if join_request
            else "完成验证后自动恢复发言。只有你本人能够操作。"
        )
        + (
            "\n验证失败会拒绝本次申请。"
            if join_request
            else "\n超时或错误次数用尽将永久禁言，可联系管理员放行。"
            if failure_action == "permanent_mute"
            else "\n超时或错误次数用尽将踢出群聊，可重新加入验证。"
        )
    )
