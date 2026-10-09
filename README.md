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

日本語フォントは macOS ならヒラギノ（明朝/角ゴシック）、Linux なら Noto CJK を自動で使う。見つからない場合は MuPDF 内蔵の CJK フォントにフォールバックする。`--serif-font` / `--sans-font` / `--sans-bold-font` で任意のフォントファイルを指定できる。

## 使い方

```sh
# Claude API（ANTHROPIC_API_KEY が必要。既定モデル claude-opus-5-5）
uv run pdf-translate "High Performance MySQL.pdf" -o mysql.ja.pdf

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
| `--backend` | `anthropic`（既定）/ `claude-cli` / `dummy` |
| `--model` | 使用モデル |
| `--effort` | anthropic バックエンドの effort（既定 `low`） |
| `--no-fallback` | anthropic バックエンドのサーバー側 refusal フォールバックを無効にする |
| `--glossary` | 用語集ファイル（例: `slave: レプリカ` を1行ずつ）をプロンプトに追加 |
| `--workers` | 同時リクエスト数（既定 4） |
| `--font-scale` | 日本語の基準文字サイズ（原文比、既定 0.85）。枠に収まらない段落だけさらに縮小する |
| `--no-toc` | しおりを翻訳しない |

anthropic バックエンドでは、安全分類器による拒否時に別モデルで自動再実行するサーバー側フォールバック（`fallbacks: "default"`）を既定で有効にしている。

## 仕組み

1. PyMuPDF でページのテキストをブロック/行/スパン単位で取得し、段落・表セルなどの翻訳単位に分ける
2. 翻訳単位をまとめて LLM に送り、JSON で訳文を受け取る（構造化出力）
3. 翻訳した部分の文字だけを redaction で削除する（画像・ベクター図形は削除しない）
4. 訳文を HTML として MuPDF の Story でレイアウトし、元の位置に配置する。全訳文を一つの中間 PDF に描いてから各ページに貼るため、日本語フォントは文書全体で一度だけ埋め込まれる
