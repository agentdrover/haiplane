"""Расшифровка кода заказа хаба на хосте исполнителя (#1712) — эталон контракта.

Хаб выдаёт хосту ``code_ciphertext`` в ответе
``GET /api/executor-hosts/<имя>/orders``: одноразовый код implementer,
зашифрованный на открытый ключ хоста — RSA-OAEP, SHA-256 и в маске MGF1, и в
хеше, метки нет — и записанный как base64url без заполнения. Расшифровать
может только владелец закрытого ключа. Открытого кода хаб не хранит нигде.

Контракт хоста по шагам:

1. ``GET /api/executor-hosts/<имя>/orders?wait=60`` ключом хоста (роль
   slot_runner); заказ может прийти повторно, пока хост не подтвердил, —
   дедуплицировать по ``id``.
2. Расшифровать ``code_ciphertext`` закрытым ключом хоста (эта функция).
3. Подставить код в ``order_text`` вместо ``{{PAIR_CODE}}`` и обменять его:
   ``POST /api/auth/chat-pair/redeem {"code": ...}`` — получится сессия
   ``ahcp_`` с channel=slot, привязанная к задаче и поколению заказа.
4. ``POST .../orders/<id>/ack {"result": "redeemed"}``; при провале обмена —
   ``redeem_failed``, при потере ответа после обмена — ``redeem_lost``.
5. Ключ slot_runner в виртуальную машину исполнителя НЕ попадает: в ВМ уходит
   только сессия ``ahcp_``.
6. По завершении прогона — ``POST .../orders/<id>/report`` с
   ``{"outcome": "submitted|rescued|failed", "sha", "branch", "note"}``.

Запуск как утилита: код печатается в stdout, шифротекст читается из stdin или
из ``--ciphertext``; закрытый ключ — файл PEM (``--key``) без пароля.
Расшифрованный код не пишется никуда, кроме stdout.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PAIR_CODE_PLACEHOLDER = "{{PAIR_CODE}}"


def decode_ciphertext(ciphertext: str) -> bytes:
    """base64url без заполнения (допускается и с заполнением)."""
    text = ciphertext.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def decrypt_code(private_key_pem: bytes, ciphertext: str) -> str:
    """Код implementer из ``code_ciphertext`` — закрытым ключом хоста."""
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("нужен закрытый ключ RSA")
    plain = key.decrypt(
        decode_ciphertext(ciphertext),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return plain.decode()


def fill_order_text(order_text: str, code: str) -> str:
    """Текст заказа для исполнителя: плейсхолдер заменён кодом."""
    return order_text.replace(PAIR_CODE_PLACEHOLDER, code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key", required=True, type=Path, help="закрытый ключ PEM")
    parser.add_argument(
        "--ciphertext", default="", help="code_ciphertext; пусто — читать из stdin"
    )
    args = parser.parse_args(argv)
    ciphertext = args.ciphertext or sys.stdin.read()
    try:
        print(decrypt_code(args.key.read_bytes(), ciphertext))
    except (ValueError, OSError) as exc:
        print(f"не удалось расшифровать: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
