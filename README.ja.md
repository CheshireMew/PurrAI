<!-- readme-header:start -->

<p align="center">
  <img src="./docs/assets/logo.svg" width="88" alt="PurrAI">
</p>

<h1 align="center">PurrAI</h1>

<p align="center">
  <strong>サーバーで常駐する Telegram グループ管理ボット。入室認証、広告対策、メッセージ保存、任意の AI チャットに対応。</strong>
</p>

<p align="center">
  <a href="./README.md">中文</a> · <a href="./README.en.md">English</a> · <strong>日本語</strong> | <a href="./BOT_GUIDE.md">文档</a> | <a href="./CONTRIBUTING.md">贡献</a> | <a href="https://github.com/CheshireMew/PurrAI/issues">反馈</a>
</p>

<p align="center">
  <a href="https://x.com/0xCheshire" title="X"><img src="https://img.shields.io/badge/X-%400xCheshire-000000?logo=x&amp;logoColor=white" alt="X：@0xCheshire"></a>
  <a href="https://t.me/CheshireBTC" title="Telegram"><img src="https://img.shields.io/badge/Telegram-CheshireBTC-26A5E4?logo=telegram&amp;logoColor=white" alt="Telegram：CheshireBTC"></a>
  <a href="https://blog.blacknico.com/" title="Blog"><img src="https://img.shields.io/badge/Blog-blog.blacknico.com-2E7D32?logo=rss&amp;logoColor=white" alt="博客：blog.blacknico.com"></a>
  <a href="https://blacknico.com/" title="Homepage"><img src="https://img.shields.io/badge/Home-blacknico.com-1F6FEB?logo=googlechrome&amp;logoColor=white" alt="个人主页：blacknico.com"></a>
</p>

<!-- readme-header:end -->

## 自分のグループを見守る常駐管理ボット

PurrAI はサーバー上で動作し、所有者が承認した Telegram グループに入室認証、広告対策、メンバー管理、メッセージ保存、任意の AI チャットを提供します。日常の管理はボットとの個別チャットやグループ内コマンドで行い、Python サービスを常時稼働させます。Web 管理画面は必要に応じて利用できます。

| やりたいこと | 入口と結果 |
| --- | --- |
| 新規メンバーを認証する | 画像の6桁の数字と一致する答えを6つの選択肢から選ぶと発言可能に。参加申請は個別チャットで認証してから承認できます |
| 広告や連投に対応する | 名前、本文、ドメイン、招待リンク、誘導アカウントのルールをグループごとに設定。ピンインや妨害文字の除去にも対応し、検出理由と実行結果を確認できます |
| メンバーを管理する | 個別チャットで `/admin`、またはグループで対象の発言に返信して `/mute`、`/ban`、`/release` |
| メッセージや削除済み広告を探す | 保存した原文、編集履歴、処理結果を検索し、JSONL で出力 |
| ボットと会話する | モデル API を設定してグループチャットを有効化。呼び名、@、返信、自発的な参加、ステッカーに対応 |

以下にサーバーへの導入手順を示します。詳しい [Bot ガイド](BOT_GUIDE.md)と[運用・開発ガイド](docs/OPERATIONS.md)、管理画面は現在中国語です。

## サーバーへの導入

主な構成は **Linux + systemd + Python 3.12** です。単一のサービスプロセスが Telegram のロングポーリングで更新を受信します。Bot のみで利用する場合は Token を設定し、所有者によるグループ承認と保護の有効化を行います。Node.js、Web ビルド、OIDC、HTTPS ドメインは Web 管理画面を使う場合に用意します。

[systemd テンプレート](deploy/purrai.service)は `/opt/purrai`、`/etc/purrai.env`、`/var/lib/purrai` を使用します。既存環境ではパスとアカウントを維持し、テンプレートも合わせて変更してください。Windows は[ローカル開発](docs/OPERATIONS.md#windows-本地开发)用の選択肢です。

### 1. ソースと依存関係の準備

サーバーに Python 3.12 を用意し、取得権限のあるソースを `/opt/purrai` に配置します。サービスアカウントがソースを読み取り、仮想環境を実行できるようにして、そのディレクトリで次を実行します。

```bash
cd /opt/purrai
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
```

### 2. ボットと保存先の設定

新規導入時は以下でサービスアカウントと設定ファイルを作成します。既存アカウントや設定ファイルは作り直さず、そのまま編集してください。

```bash
sudo useradd --system --user-group --home-dir /var/lib/purrai --shell /usr/sbin/nologin purrai
sudo install -d -o purrai -g purrai /var/lib/purrai /var/lib/purrai/backups
sudo install -o root -g purrai -m 0640 .env.example /etc/purrai.env
sudoedit /etc/purrai.env
```

BotFather で取得した Token を設定し、データベースとバックアップのパスを以下に合わせます。他の項目は [.env.example](.env.example)を参照してください。

```dotenv
PURRAI_BOT_TOKEN=your_bot_token
PURRAI_DATABASE_URL=sqlite+aiosqlite:////var/lib/purrai/purrai.db
PURRAI_BACKUP_DIR=/var/lib/purrai/backups
PURRAI_SUPERADMIN_IDS=[]
```

特権管理者は後から設定できます。稼働中のボットに個別チャットで `/id` を送り、返された数字を `PURRAI_SUPERADMIN_IDS=[123456789]` に設定して再起動すると、Bot パネルからグループを承認できます。ユーザー名は数字 ID の代わりになりません。サービスを停止して [CLI で承認](docs/OPERATIONS.md#批准或撤销群接入)する方法もあります。

### 3. 初期化して常駐サービスを起動

手動の CLI は作業ディレクトリの `.env` を読み、systemd は `/etc/purrai.env` を読みます。移行時はサービスと同じ保存先を明示します。

```bash
sudo -u purrai env \
  PURRAI_DATABASE_URL=sqlite+aiosqlite:////var/lib/purrai/purrai.db \
  PURRAI_BACKUP_DIR=/var/lib/purrai/backups \
  /opt/purrai/.venv/bin/purrai init-db
sudo install -m 0644 deploy/purrai.service /etc/systemd/system/purrai.service
sudo systemctl daemon-reload
sudo systemctl enable --now purrai
sudo systemctl status purrai
```

Telegram に接続した後、サーバー上の `http://127.0.0.1:8765/health/ready` を確認します。200 はデータベース、バックグラウンド処理、ボット接続が準備できた状態です。ログは `journalctl -u purrai -n 100 --no-pager`、設定変更後の再起動は `sudo systemctl restart purrai` を使います。

## 最初のグループを接続する

1. テスト用スーパーグループにボットを追加し、メッセージ削除、メンバー制限、追放の権限を持つ管理者にします。同じ Token のロングポーリングは1つだけ稼働させます。既存の Webhook がある場合は用途を確認してから手動で解除します。
2. 所有者の個別 `/admin` パネル、または停止中の CLI でグループを承認します。ボットの追加、管理者権限の付与、旧データベースの移行だけでは利用承認されません。
3. グループ所有者または管理者がボットに `/admin` を送り、対象を選んで **「检查并启用群保护」**（権限確認と保護の有効化）を押します。ルール、ホワイトリスト、認証方針を設定し、テストグループで認証、時間切れ、手動解除、広告処理を確認します。

管理権限は Telegram の現在の管理者情報で確認します。グループ所有者と管理者は認証とすべての自動制限を免除され、特権管理者は利用承認とシステム保守を担当します。各ボタンとコマンドは [Bot ガイド](BOT_GUIDE.md)にまとめています。

## 初期設定での動作

| 状況 | 初期設定の処理 |
| --- | --- |
| 入室認証 | 180秒以内、誤答は最大3回。誤答時に問題を変更しますが期限は延長せず、選択肢は再起動後も有効です |
| 認証失敗 | グループに残したまま永久ミュート。再参加可能な退出処理へ変更も可能。参加申請は拒否し、再申請まで10分待機 |
| 名前・ユーザー名の違反 | 入室時に永久ミュート。発言時の検出ではその発言も削除 |
| 本文・リンク・連投の違反 | 初期設定では削除のみ。キーワードごとに削除と永久ミュートを選択可能。ルール一覧は最初は空です |
| 明確な勧誘・外部グループのリンク | 明確な取引勧誘、確認済みの外部グループ、外部の非公開招待は削除と永久BAN。本グループと許可リストを除外し、通報の引用は別扱い |
| 連絡先カードの共有 | 一般メンバーは在籍期間に関係なく発言を削除し、永久BAN。再参加は禁止 |
| 新規メンバーの観察期間 | 3日間と最初の3件。リンク、@アカウント・グループ、ユーザー・チャンネルの転送を削除。ウォレット・トークンのコントラクトアドレスは在籍期間に関係なく一般メンバー全員が対象 |
| 新規メンバーの画像制限 | 信頼できる入室記録があるメンバーが対象。異なるテキストのみの発言3件、または在籍15日で解除。この制限だけなら削除のみ |
| 名前横のステータス絵文字 | 初期リストは `bygbb666`、`GsHeChaba`、`TGVIP_16`。参加時の一致は永久BAN、発言時の一致は永久ミュート。群ごとに変更可能 |
| 管理者による手動ミュート解除 | グループのホワイトリストに追加し、待機中の処罰を取り消して以後の認証と自動処理を免除 |

「処理待ち」は受付済みを表し、Telegram の確認後に「完了」となります。他の管理者が設定した制限は自動的に上書きしません。削除済みの発言は元の場所に復元できません。画像認証は自動化された参加の負担を増やすもので、OCR や人による回答を完全には防げません。

## メッセージ保存と AI チャット

**メッセージ保存は初期状態で有効**で、グループ保護とは独立しています。承認済みグループの新規メッセージ、編集履歴、ボット削除前の広告原文を容量・版数の上限内で保存します。画像や動画はファイル ID とメタデータのみです。`/admin` → グループ → **「消息归档与导出」** から検索と JSONL 出力ができます。範囲と形式は [MESSAGE_ARCHIVE.md](MESSAGE_ARCHIVE.md)を参照してください。

**AI チャットはグループごとに有効化**します。サーバーで `PURRAI_CHAT_API_KEY` を設定し、必要に応じて `PURRAI_CHAT_API_BASE` と `PURRAI_CHAT_MODEL` を変更します。再起動後、チャット設定で呼び名、性格、参加方法を調整します。モデルの初期値は [.env.example](.env.example)が基準です。全体・グループ・個人の日次上限に達すると AI リクエストを止め、認証と広告対策は継続します。

会話は最近のテキストと返信関係を使い、グループとトピックごとに文脈を分けます。有効化すると関連する内容が設定先のモデル事業者へ送られます。送信済みの履歴は後から編集・削除されても書き換えません。チャットはメディアをダウンロードせず、管理操作も実行しません。詳細は [Bot ガイド](BOT_GUIDE.md)と[利用量・保存制限](docs/OPERATIONS.md#ai-聊天与存储额度)を参照してください。

## Web 管理画面と保守

| 目的 | 参照先 |
| --- | --- |
| 任意の Web 管理画面 | [フロントエンド、HTTPS、Telegram OIDC の設定](docs/OPERATIONS.md#可选网页)。ビルドには Node.js 24 が必要です |
| バックアップ・更新・復元 | [運用手順](docs/OPERATIONS.md#备份升级与恢复)。SQLite の整合性のあるバックアップを使い、移行前に停止します |
| 稼働状態と失敗した処理 | [稼働確認](docs/OPERATIONS.md#运行检查与任务状态)、システム状態、操作記録 |
| コード変更・ローカル調査 | [貢献ガイド](CONTRIBUTING.md)と [Windows 開発](docs/OPERATIONS.md#windows-本地开发) |
| 設計と検証範囲 | [実装計画](IMPLEMENTATION_PLAN.md)・[検証記録](VALIDATION.md) |

バックエンドは aiogram 3、FastAPI、SQLAlchemy、SQLite、Web は Vue 3 と Element Plus を使用します。ソースは `src/purrai` と `frontend/src`、移行は `migrations`、サーバーテンプレートは `deploy` にあります。

ボットが扱えるのは Telegram が実際に配信した更新だけです。参加前の履歴取得、全既存メンバーの列挙、他の人が手動削除した通常の発言の把握は保証できません。自動テストは Telegram、OIDC、バックエンド API を模擬しており、サーバーと実グループの動作は対象環境で確認が必要です。

## ライセンスと第三者リソース

プロジェクトのコードライセンスは現時点で指定されていません。第三者フォントには同梱の許諾が適用されます。認証画像には [Noto Sans](https://github.com/notofonts/latin-greek-cyrillic)（`src/purrai/assets/NotoSans.ttf`）を使用し、著作権は The Noto Project Authors に帰属します。同梱の [SIL Open Font License 1.1](src/purrai/assets/OFL.txt)に基づき配布します。
