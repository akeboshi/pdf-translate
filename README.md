# pdf-translate

英語の PDF を日本語に翻訳し、元のレイアウトのまま PDF として出力するツール。

- 本文テキストだけを削除して同じ位置に日本語を書き込むため、図・画像・罫線・表の枠・背景はそのまま残る
- コード（等幅フォント）・数値だけのテキストは翻訳しない
- 斜体・太字・インラインコード・上付き（脚注番号）・文字色は HTML タグとして翻訳に引き継ぐ
- 表のセルや柱（ヘッダ/フッタ）は行単位、本文は段落単位で翻訳する
- 日本語が元の枠に収まらない場合は右・下の空きへ広げ、それでも収まらなければ縮小する
- リンク（目次・相互参照・URL）としおり（ブックマーク）を維持し、しおりも翻訳する
- 翻訳結果は `<出力>.cache.jsonl` にキャッシュされ、中断後の再実行や再レイアウトで API を再度呼ばない

## セットアップ

```sh
uv sync
```

日本語フォントは macOS ならヒラギノ（明朝/角ゴシック）、Windows なら游明朝・游ゴシック、Linux なら Noto CJK を自動で使う。見つからない場合は MuPDF 内蔵の CJK フォントにフォールバックする。`--serif-font` / `--sans-font` / `--sans-bold-font` で任意のフォントファイルを指定できる。

## 使い方

### GUI

引数なしで起動すると GUI が開く。PDF を選び、翻訳エンジンとページ範囲を指定して「翻訳を開始」を押す。進捗はバーとログに表示され、途中で中止しても翻訳済みの部分はキャッシュに残るので、再開すると続きから翻訳する。

```sh
uv run pdf-translate
```

翻訳エンジンに Antigravity CLI を選んで `agy` が見つからない場合は、公式インストーラーでインストールするか確認する。インストール後はサインイン用のウィンドウを開くので、サインインを済ませてから翻訳を開始する。

### コマンドライン

```sh
# Claude API（ANTHROPIC_API_KEY が必要。既定モデル claude-opus-5-5）
uv run pdf-translate "High Performance MySQL.pdf" -o mysql.ja.pdf

# Antigravity CLI (`agy`) を使う（事前に `agy` を一度起動してサインインしておく）
# モデル未指定なら `agy models` の中で最新の Gemini Flash を使う（--effort の low/medium/high に合わせて選ぶ）
uv run pdf-translate "High Performance MySQL.pdf" --backend agy

# API キーの代わりにローカルの Claude Code (`claude -p`) を使う
uv run pdf-translate "High Performance MySQL.pdf" --backend claude-cli --model sonnet

# まず数ページだけ試す（選んだページだけを出力）
uv run pdf-translate "High Performance MySQL.pdf" --pages 19-30 --only-selected -o sample.pdf

# LLM を呼ばずにレイアウトだけ確認する
uv run pdf-translate "High Performance MySQL.pdf" --backend dummy --pages 1-40
```

主なオプション:

| オプション | 説明 |
| --- | --- |
| `--pages` | 翻訳するページ（1 始まり。例: `1-20,25,30-`） |
| `--only-selected` | `--pages` で選んだページだけを出力する |
| `--backend` | `anthropic`（既定）/ `agy` / `claude-cli` / `dummy` |
| `--model` | 使用モデル |
| `--effort` | anthropic バックエンドの effort（既定 `low`） |
| `--no-fallback` | anthropic バックエンドのサーバー側 refusal フォールバックを無効にする |
| `--glossary` | 用語集ファイル（例: `slave: レプリカ` を1行ずつ）をプロンプトに追加 |
| `--workers` | 同時リクエスト数（既定 4） |
| `--font-scale` | 日本語の基準文字サイズ（原文比、既定 0.85）。枠に収まらない段落だけさらに縮小する |
| `--no-toc` | しおりを翻訳しない |

anthropic バックエンドでは、安全分類器による拒否時に別モデルで自動再実行するサーバー側フォールバック（`fallbacks: "default"`）を既定で有効にしている。

## 配布版（Windows / macOS）

GitHub Actions の `Build` ワークフローで、Windows 版と macOS 版を作る。

- 手動実行（workflow_dispatch）すると、ワークフローの成果物としてダウンロードできる
- `v1.0.0` のような `v` で始まるタグを push すると、そのタグ名でリリースページを作り、両方の zip を添付する

```sh
git tag v1.0.0
git push origin v1.0.0
```

| ファイル | 中身 |
| --- | --- |
| `pdf-translate-windows-x64.zip` | `pdf-translate.exe`（1 ファイル）。ダブルクリックで GUI、コマンドプロンプトから引数付きで実行するとコマンドライン |
| `pdf-translate-macos-arm64.zip` | `pdf-translate.app`（Apple シリコン用）。ダブルクリックで GUI。コマンドラインは `pdf-translate.app/Contents/MacOS/pdf-translate` |

どちらも署名していないため、初回起動時に OS の警告が出る。

- Windows: SmartScreen の「詳細情報」→「実行」
- macOS: アプリを右クリック →「開く」。それでも開けない場合は `xattr -dr com.apple.quarantine pdf-translate.app`

日本語フォントは Windows では游明朝・游ゴシック（なければ MS 明朝・MS ゴシック）、macOS ではヒラギノを使う。

手元でビルドする場合（Windows は Windows 上、macOS は macOS 上でのみビルドできる）:

```sh
uv sync --dev
# Windows
uv run pyinstaller --noconfirm --onefile --name pdf-translate --collect-all pymupdf packaging/entry.py
# macOS
uv run pyinstaller --noconfirm --windowed --name pdf-translate --collect-all pymupdf packaging/entry.py
```

## 仕組み

1. PyMuPDF でページのテキストをブロック/行/スパン単位で取得し、段落・表セルなどの翻訳単位に分ける
2. 翻訳単位をまとめて LLM に送り、JSON で訳文を受け取る（構造化出力）
3. 翻訳した部分の文字だけを redaction で削除する（画像・ベクター図形は削除しない）
4. 訳文を HTML として MuPDF の Story でレイアウトし、元の位置に配置する。全訳文を一つの中間 PDF に描いてから各ページに貼るため、日本語フォントは文書全体で一度だけ埋め込まれる
