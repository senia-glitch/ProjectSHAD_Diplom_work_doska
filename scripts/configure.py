"""Generate local secrets without overwriting existing configuration."""
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / '.env'
    if target.exists():
        print('Existing .env preserved')
        return
    text = (ROOT / '.env.example').read_text(encoding='utf-8')
    text = text.replace('POSTGRES_PASSWORD=__GENERATE__', 'POSTGRES_PASSWORD=' + secrets.token_hex(24))
    text = text.replace('CORE_JWT_SECRET=__GENERATE__', 'CORE_JWT_SECRET=' + secrets.token_hex(32))
    with target.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(text)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    print('Created local .env; secrets are not printed')


if __name__ == '__main__':
    main()
