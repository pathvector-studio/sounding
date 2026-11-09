"""UI labels in ``profile.profile.language`` (ja or en; anything else renders as en)."""

from __future__ import annotations

from typing import Any

LABELS: dict[str, dict[str, Any]] = {
    "en": {
        "lang": "en", "app": "Sounding", "nav_digest": "Latest", "nav_digests": "Digests", "nav_status": "Status",
        "logout": "Log out", "login": "Log in", "password": "Password", "login_failed": "Wrong password.",
        "login_throttled": "Too many failed attempts. Try again in {n} seconds.",
        "banner": "No password is set (WEB_PASSWORD_HASH is empty): anyone who can reach this page can read it. "
                  "Keep it behind Tailscale or an access proxy, or set a password (python -m sounding hash-password).",
        "no_digest": "No digest yet. The worker builds one on the schedule in your profile; run "
                     "`python -m sounding digest --once` to make the first one now.",
        "digest": "Digest", "period": "Period", "language": "Language", "created": "Created", "all_digests": "All digests",
        "health": "Health", "conversations": "Active conversations", "events": "Procedural updates", "articles": "Related articles",
        "quiet": "No activity in this period (the digest ran normally).",
        "new_messages": "{n} new", "changed": "Changed since last", "no_state": "(no summary yet)",
        "participants": "Participants", "details": "Details", "latest_message": "Latest message", "open_source": "Open",
        "health_msg": {
            "job_failed": "Job failed: {d}", "budget_skipped": "{d} conversation(s) not summarized: daily LLM budget exceeded",
            "llm_disabled": "LLM is disabled (LLM_PROVIDER=none): no summaries were made",
        },
        "prev": "Newer", "next_page": "Older", "no_digests": "No digests yet.",
        "conversation": "Conversation", "mailing_list": "List", "messages": "messages", "first": "First message", "last": "Last message",
        "merged": "This conversation was merged into another one.", "merged_link": "Open the merged conversation",
        "no_summary": "No summary has been generated for this conversation in this language yet.",
        "version": "version", "issues": "Issues", "agreed": "Agreed", "open_questions": "Open questions", "next": "Next",
        "sources": "Sources", "history": "Version history", "model": "Model", "selection": "Why it was (not) selected",
        "included": "included", "excluded": "not included", "score": "score", "decided_by": "decided by",
        "status_names": {"agreed": "agreed", "leaning": "leaning", "open": "open", "parked": "parked"},
        "status_page": "Status", "jobs": "Jobs", "job": "Job", "last_ok": "Last ok", "last_failed": "Last failed", "error": "Error",
        "source_list": "Sources", "kind": "Kind", "key": "Key", "enabled": "Enabled", "last_fetch": "Last fetch", "state": "State",
        "items_n": "Items", "conv_n": "Conversations", "events_n": "Events", "yes": "yes", "no": "no", "never": "never", "none": "-",
        "llm": "LLM today", "provider": "Provider", "spend": "Spent", "budget": "Budget", "unlimited": "unlimited", "calls": "Calls",
        "deliveries": "Deliveries", "channel": "Channel", "last_attempt": "Last attempt", "attempts": "Attempts", "no_deliveries": "Nothing has been sent yet.",
        "last_digest": "Last digest", "atom": "Atom feed", "atom_help": "Subscribe to this URL in a feed reader. Anyone who knows it can read the digests; keep it private.",
        "not_found": "Not found", "not_found_text": "There is nothing here.", "home": "Back to the latest digest",
    },
    "ja": {
        "lang": "ja", "app": "Sounding", "nav_digest": "最新", "nav_digests": "ダイジェスト一覧", "nav_status": "状態",
        "logout": "ログアウト", "login": "ログイン", "password": "パスワード", "login_failed": "パスワードが違います。",
        "login_throttled": "失敗が続いたため受け付けません。{n} 秒後にやり直してください。",
        "banner": "パスワードが未設定です（WEB_PASSWORD_HASH が空）。このページに届く人は誰でも読めます。"
                  "Tailscale やアクセスプロキシの内側で使うか、パスワードを設定してください（python -m sounding hash-password）。",
        "no_digest": "まだダイジェストがありません。worker がプロファイルのスケジュールで作ります。"
                     "今すぐ作るには `python -m sounding digest --once` を実行してください。",
        "digest": "ダイジェスト", "period": "期間", "language": "言語", "created": "作成", "all_digests": "ダイジェスト一覧",
        "health": "健康状態", "conversations": "動きがあった会話", "events": "手続きの更新", "articles": "関連記事",
        "quiet": "この期間は動きがありませんでした（ダイジェストは正常に動いています）。",
        "new_messages": "新着 {n} 件", "changed": "前回から", "no_state": "（まだ要約なし）",
        "participants": "参加者", "details": "詳細", "latest_message": "最新のメッセージ", "open_source": "開く",
        "health_msg": {
            "job_failed": "ジョブが失敗: {d}", "budget_skipped": "{d} 件の会話を要約していません（LLM の日次予算超過）",
            "llm_disabled": "LLM が無効です（LLM_PROVIDER=none）。要約は作られていません",
        },
        "prev": "新しい方", "next_page": "古い方", "no_digests": "まだダイジェストがありません。",
        "conversation": "会話", "mailing_list": "リスト", "messages": "件", "first": "最初のメッセージ", "last": "最新のメッセージ",
        "merged": "この会話は別の会話に統合されました。", "merged_link": "統合先の会話を開く",
        "no_summary": "この言語の要約はまだ作られていません。",
        "version": "版", "issues": "論点", "agreed": "合意済み", "open_questions": "未決着", "next": "次",
        "sources": "出典", "history": "版の履歴", "model": "モデル", "selection": "選別の理由",
        "included": "採用", "excluded": "不採用", "score": "スコア", "decided_by": "判定",
        "status_names": {"agreed": "合意", "leaning": "傾き", "open": "未決着", "parked": "保留"},
        "status_page": "状態", "jobs": "ジョブ", "job": "ジョブ", "last_ok": "最後の成功", "last_failed": "最後の失敗", "error": "エラー",
        "source_list": "情報源", "kind": "種類", "key": "キー", "enabled": "有効", "last_fetch": "最後の取得", "state": "状態",
        "items_n": "項目", "conv_n": "会話", "events_n": "イベント", "yes": "はい", "no": "いいえ", "never": "なし", "none": "-",
        "llm": "LLM（今日）", "provider": "プロバイダ", "spend": "使用額", "budget": "上限", "unlimited": "無制限", "calls": "呼び出し",
        "deliveries": "配信", "channel": "チャネル", "last_attempt": "最後の試行", "attempts": "試行", "no_deliveries": "まだ何も送っていません。",
        "last_digest": "最新のダイジェスト", "atom": "Atom フィード", "atom_help": "フィードリーダーにこの URL を登録します。URL を知っている人はダイジェストを読めるので、公開しないでください。",
        "not_found": "見つかりません", "not_found_text": "ここには何もありません。", "home": "最新のダイジェストへ",
    },
}


def labels(language: str | None) -> dict[str, Any]:
    base = (language or "en").split("-")[0].lower()
    return LABELS.get(base, LABELS["en"])
