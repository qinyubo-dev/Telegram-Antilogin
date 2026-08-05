# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import logging
import os
import json
import re
import secrets
import time
import copy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from telethon import TelegramClient, events
from telethon.tl.custom import Button

from accounts.account_manager import AccountManager, user_accounts
from payments.payment_system import PaymentSystem
from storage.admin_audit import AdminAuditLog, audit_file_path, mask_phone, phone_digest
from storage.data_manager import DataManager
from storage.user_profile_cache import UserProfileCache
from handlers.handler_utils import back_button, clear_state, edit_or_respond, get_state, paginate_items, pagination_buttons, require_admin, safe_edit, set_state
from localization import localized_result, t
from accounts import account_runtime
from reminders.login_unlock_reminder import (
    ReminderScheduleValidationError,
    format_offset,
    parse_schedule_offsets,
)
import settings as config


logger = logging.getLogger(__name__)
_ADMIN_ACTION_TTL_SECONDS = 5 * 60
_pending_admin_actions = {}
_ADMIN_INPUT_STATE_KEYS = (
    "admin_user_search",
    "admin_order_search",
    "admin_audit_filter",
    "admin_user_subscription_input",
    "target_user_id",
    "plan_id",
    "admin_subscription_config_flow",
    "admin_login_unlock_reminder_count",
)
_ADMIN_NAVIGATION_STATE_KEYS = (
    "admin_user_results",
    "admin_user_back",
    "admin_order_query",
    "admin_order_back",
    "admin_audit_filters",
)
_AUDIT_ACTION_CODES = (
    "user.search",
    "user.detail",
    "order.list",
    "order.search",
    "order.detail",
    "order.recheck",
    "order.retry_fulfillment",
    "subscription.grant",
    "subscription.delete",
    "accounts.reload",
    "accounts.resume",
    "accounts.suspend",
    "audit.download",
    "config.expiry_reminder_set",
    "config.login_unlock_reminder_schedule_set",
    "config.subscription_catalog_set",
    "config.subscription_discounts_set",
)
_AUDIT_FILTER_FIELDS = {
    "admin": "admin_id",
    "管理员": "admin_id",
    "action": "action",
    "动作": "action",
    "target": "target_id",
    "目标": "target_id",
}
_SUBSCRIPTION_CONFIG_FIELDS = {
    "go": (
        ("price", "admin.config.field.go_price", "decimal", "0.6"),
        ("quota", "admin.config.field.go_quota", "integer", "2"),
    ),
    "plus": (
        ("price", "admin.config.field.plus_price", "decimal", "1.5"),
        ("quota", "admin.config.field.plus_quota", "integer", "10"),
        ("addon_unit_price", "admin.config.field.plus_addon_price", "decimal", "0.1"),
        ("min_addon", "admin.config.field.plus_min_addon", "integer", "5"),
    ),
    "pro": (
        ("price", "admin.config.field.pro_price", "decimal", "3"),
    ),
    "discounts": (
        ("90", "admin.config.field.discount_90", "discount", "5"),
        ("180", "admin.config.field.discount_180", "discount", "10"),
        ("365", "admin.config.field.discount_365", "discount", "15"),
    ),
}


def _language(user_id: int) -> str:
    return DataManager.get_user_language(user_id)


def _localized_code(language: str, prefix: str, value, fallback=None) -> str:
    raw = str(value if value not in (None, "") else (fallback or ""))
    try:
        return t(language, f"{prefix}.{raw}")
    except KeyError:
        return raw or t(language, "admin.common.unknown")


def _localized_reason(language: str, value) -> str:
    raw = str(value or "")
    if not raw:
        return t(language, "admin.common.unknown")
    try:
        return t(language, f"admin.reason.{raw}")
    except KeyError:
        return localized_result(language, raw)


def _audit_action_code(value, language: str) -> str:
    """Resolve a localized audit action label back to its stored action code."""
    raw = str(value or "").strip()
    folded = raw.casefold()
    for action in _AUDIT_ACTION_CODES:
        if folded == action.casefold():
            return action
        for candidate_language in dict.fromkeys((language, "zh", "en")):
            if folded == _localized_code(
                candidate_language, "admin.audit.action", action
            ).casefold():
                return action
    return raw


def _parse_audit_filters(value, language: str) -> dict:
    """Parse localized audit filters while preserving legacy English syntax."""
    text = str(value or "").strip()
    filters = {"exclude_attempt": True}
    field_pattern = "|".join(
        re.escape(field) for field in sorted(_AUDIT_FILTER_FIELDS, key=len, reverse=True)
    )
    matches = re.finditer(
        rf"(?:^|\s)({field_pattern})\s*[:：]\s*(.*?)"
        r"(?=\s+\S+\s*[:：]|$)",
        text,
        re.IGNORECASE,
    )
    for match in matches:
        field = _AUDIT_FILTER_FIELDS[match.group(1).casefold()]
        parsed = match.group(2).strip()
        if not parsed:
            continue
        filters[field] = (
            _audit_action_code(parsed, language) if field == "action" else parsed
        )

    if len(filters) == 1:
        action = _audit_action_code(text, language)
        if action in _AUDIT_ACTION_CODES:
            filters["action"] = action
    return filters


def _json_display(value) -> str:
    if value in (None, {}, []):
        return "-"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _format_audit_entries(entries, language: str) -> str:
    blocks = [t(language, "admin.audit.detail_title")]
    for index, item in enumerate(entries, 1):
        blocks.append(t(
            language,
            "admin.audit.entry",
            index=index,
            timestamp=item.get("timestamp") or "-",
            admin_id=item.get("admin_id") or "-",
            action=_localized_code(language, "admin.audit.action", item.get("action")),
            target_type=_localized_code(
                language, "admin.audit.target", item.get("target_type"), "system"
            ),
            target_id=item.get("target_id") or "-",
            result=_localized_code(
                language, "admin.audit.result", item.get("result"), "attempt"
            ),
            before=_json_display(item.get("before")),
            after=_json_display(item.get("after")),
            metadata=_json_display(item.get("metadata")),
            error=_localized_reason(language, item.get("error")) if item.get("error") else "-",
        ))
    return "\n\n".join(blocks)


def _clear_admin_input_state(user_id: int) -> None:
    clear_state(user_id, *_ADMIN_INPUT_STATE_KEYS)


def _clear_admin_state(user_id: int) -> None:
    clear_state(user_id, *_ADMIN_INPUT_STATE_KEYS, *_ADMIN_NAVIGATION_STATE_KEYS)


def _config_flow_current_value(flow, field):
    before = flow["before"]
    if flow["target"] == "discounts":
        return before[int(field)]["discount_percent"]
    return before[flow["target"]][field]


def _config_flow_prompt(flow, error=None, language="zh"):
    fields = _SUBSCRIPTION_CONFIG_FIELDS[flow["target"]]
    field, label_key, _value_type, example = fields[flow["index"]]
    target_name = (
        t(language, "admin.config.long_discounts")
        if flow["target"] == "discounts" else flow["target"].upper()
    )
    prefix = f"❌ {error}\n\n" if error else ""
    return t(
        language,
        "admin.config.prompt",
        prefix=prefix,
        target=target_name,
        index=flow["index"] + 1,
        total=len(fields),
        label=t(language, label_key),
        current=_config_flow_current_value(flow, field),
        example=example,
    )


def _parse_config_value(value, value_type, language="zh"):
    text = str(value or "").strip()
    try:
        if value_type == "integer":
            if not re.fullmatch(r"\d+", text):
                raise ValueError(t(language, "admin.config.error.positive_integer"))
            parsed = int(text)
            if parsed <= 0:
                raise ValueError(t(language, "admin.config.error.gt_zero"))
            return parsed
        parsed = Decimal(text)
        if not parsed.is_finite():
            raise ValueError(t(language, "admin.config.error.valid_number"))
        if value_type == "discount":
            if parsed < 0 or parsed >= 100:
                raise ValueError(t(language, "admin.config.error.discount_range"))
        elif parsed <= 0:
            raise ValueError(t(language, "admin.config.error.gt_zero"))
        return DataManager._decimal_text(parsed)
    except (InvalidOperation, TypeError, ValueError) as error:
        if isinstance(error, ValueError) and str(error):
            raise
        raise ValueError(t(language, "admin.config.error.valid_number")) from error


def _config_flow_candidate(flow):
    candidate = copy.deepcopy(flow["before"])
    if flow["target"] == "discounts":
        candidate[30] = {"discount_percent": "0"}
        for days, value in flow["values"].items():
            candidate[int(days)] = {"discount_percent": value}
    else:
        candidate[flow["target"]].update(flow["values"])
    return candidate


def _config_plan_text(catalog, plan_id, language="zh"):
    plan = catalog[plan_id]
    if plan_id == "go":
        return t(language, "admin.config.plan.go", price=plan["price"], quota=plan["quota"])
    if plan_id == "plus":
        return t(
            language, "admin.config.plan.plus", price=plan["price"],
            quota=plan["quota"], addon_price=plan["addon_unit_price"],
            min_addon=plan["min_addon"],
        )
    return t(language, "admin.config.plan.pro", price=plan["price"])


def _config_flow_preview(flow, language="zh"):
    candidate = _config_flow_candidate(flow)
    if flow["target"] == "discounts":
        def discount_text(periods):
            return ("，" if language == "zh" else ", ").join(
                t(
                    language, "admin.config.discount_item", days=days,
                    percent=periods[days]["discount_percent"],
                )
                for days in (90, 180, 365)
            )

        before_text = discount_text(flow["before"])
        after_text = discount_text(candidate)
    else:
        before_text = _config_plan_text(flow["before"], flow["target"], language)
        after_text = _config_plan_text(candidate, flow["target"], language)
    return t(language, "admin.config.preview", before=before_text, after=after_text)


async def _get_user_display_name(bot, user_id: int):
    is_cached, cached_name = UserProfileCache.get(user_id)
    if is_cached:
        return cached_name

    try:
        entity = await bot.get_entity(user_id)
        name = " ".join(
            part for part in [
                getattr(entity, "first_name", None),
                getattr(entity, "last_name", None),
            ] if part
        )
        username = getattr(entity, "username", None)
        display_name = name or username
        UserProfileCache.set_profile(user_id, display_name, username)
        return display_name
    except Exception:
        return None


async def handle_admin_message(event, bot=None, payment_system: PaymentSystem = None) -> bool:
    """Handle admin text-input states. Returns True when the event was consumed."""
    user_id = event.sender_id
    state = get_state(user_id)
    language = _language(user_id)

    reminder_count = state.get("admin_login_unlock_reminder_count")
    if reminder_count:
        if not DataManager.is_admin(user_id):
            _clear_admin_input_state(user_id)
            return True
        try:
            offsets = parse_schedule_offsets(event.text, int(reminder_count))
        except ReminderScheduleValidationError as error:
            await event.respond(
                t(
                    language,
                    "admin.login_unlock.invalid",
                    error=t(language, f"admin.login_unlock.error.{error.code}"),
                    count=reminder_count,
                ),
                buttons=[[back_button(b"admin_login_unlock_reminder_settings", language=language)]],
            )
            return True

        before = DataManager.get_login_unlock_reminder_schedule()
        audit_id = AdminAuditLog.record_attempt(
            user_id,
            "config.login_unlock_reminder_schedule_set",
            "system_setting",
            "login_unlock_reminder_schedule",
        )
        if not DataManager.set_login_unlock_reminder_schedule(reminder_count, offsets):
            _audit_result(
                audit_id,
                user_id,
                "config.login_unlock_reminder_schedule_set",
                "failed",
                "system_setting",
                "login_unlock_reminder_schedule",
                before=before,
                error="save_failed",
            )
            await event.respond(
                t(language, "admin.login_unlock.save_failed"),
                buttons=[[back_button(b"admin_login_unlock_reminder_settings", language=language)]],
            )
            return True

        _clear_admin_input_state(user_id)
        after = DataManager.get_login_unlock_reminder_schedule()
        system = account_runtime.get_login_unlock_reminder_system()
        recalculated = bool(system and await system.recalculate_all())
        audited = _audit_result(
            audit_id,
            user_id,
            "config.login_unlock_reminder_schedule_set",
            "success",
            "system_setting",
            "login_unlock_reminder_schedule",
            before=before,
            after=after,
            metadata={"recalculated": recalculated},
        )
        schedule_text = " / ".join(format_offset(value, language) for value in offsets)
        notice = t(
            language,
            "admin.login_unlock.saved",
            count=reminder_count,
            schedule=schedule_text,
        )
        if not recalculated:
            notice += t(language, "admin.login_unlock.recalculate_warning")
        if not audited:
            notice += t(language, "admin.common.audit_warning")
        await event.respond(
            notice,
            buttons=[[back_button(b"admin_login_unlock_reminder_settings", language=language)]],
        )
        return True

    if state.get("admin_user_search"):
        clear_state(user_id)
        if not DataManager.is_admin(user_id) or not bot or not payment_system:
            return True
        query = (event.text or "").strip()
        audit_id = AdminAuditLog.record_attempt(
            user_id, "user.search", "user", metadata={"query_type": "lookup"}
        )
        try:
            results = await _search_admin_users(bot, payment_system, query)
            digits = _normalized_phone(query)
            metadata = {
                "result_count": len(results),
                "match_types": sorted({item["match_type"] for item in results}),
            }
            if digits and re.fullmatch(r"[\d+()\-\s]+", query):
                metadata.update({"phone_masked": mask_phone(query), "phone_hash": phone_digest(query)})
            audited = _audit_result(
                audit_id, user_id, "user.search", "success", "user",
                target_id=results[0]["user_id"] if len(results) == 1 else None,
                metadata=metadata,
            )
            if not results:
                await event.respond(
                    t(language, "admin.user.no_match"),
                    buttons=[[back_button(b"admin_panel", language=language)]],
                )
                return True
            set_state(
                user_id,
                admin_user_results=copy.deepcopy(results),
                admin_user_back="admin_user_search_results",
            )
            buttons = []
            for item in results:
                label = item.get("display_name") or (
                    f"@{item['username']}" if item.get("username") else t(
                        language, "admin.user.fallback_name", user_id=item["user_id"]
                    )
                )
                buttons.append([Button.inline(
                    f"{label} · {item['user_id']}"[:58], f"admin_user_detail_{item['user_id']}".encode()
                )])
            buttons.append([back_button(b"admin_panel", language=language)])
            warning = t(language, "admin.user.search_audit_warning") if not audited else ""
            await event.respond(
                t(language, "admin.user.search_title", count=len(results), warning=warning),
                buttons=buttons,
            )
        except Exception as error:
            _audit_result(audit_id, user_id, "user.search", "failed", "user", error=str(error))
            logger.exception("管理员用户搜索失败")
            await event.respond(t(language, "admin.user.search_failed"))
        return True

    if state.get("admin_order_search"):
        clear_state(user_id)
        if not DataManager.is_admin(user_id) or not payment_system:
            return True
        query = (event.text or "").strip()
        audit_id = AdminAuditLog.record_attempt(user_id, "order.search", "order")
        result = payment_system.list_admin_orders("all", query=query, page_size=20)
        set_state(user_id, admin_order_query=query)
        _audit_result(
            audit_id, user_id, "order.search", "success", "order",
            target_id=result["items"][0]["order_id"] if len(result["items"]) == 1 else None,
            metadata={"result_count": result["total"]},
        )
        buttons = []
        for item in result["items"]:
            callback = f"admin_order_detail_{item['order_id']}".encode()
            if len(callback) <= 64:
                buttons.append([Button.inline(
                    f"{_order_status_text(item, language)} · {item['order_id']}"[:58], callback,
                )])
        nav = pagination_buttons(
            "admin_order_search_page", result["page"], result["max_page"], language
        )
        if nav:
            buttons.append(nav)
        buttons.append([back_button(b"admin_orders_all_0", language=language)])
        await event.respond(
            t(language, "admin.order.search_result", count=result["total"]), buttons=buttons
        )
        return True

    if state.get("admin_audit_filter"):
        clear_state(user_id)
        if not DataManager.is_admin(user_id):
            return True
        filters = _parse_audit_filters(event.text, language)
        result = AdminAuditLog.query(filters, page=0, page_size=25)
        set_state(user_id, admin_audit_filters=filters)
        buttons = [
            [Button.inline(
                f"{_localized_code(language, 'admin.audit.result', item.get('result'))} · "
                f"{_localized_code(language, 'admin.audit.action', item.get('action'))} · "
                f"{item.get('target_id') or '-'}",
                f"admin_audit_detail_{item['audit_id']}".encode(),
            )]
            for item in result["items"]
        ]
        buttons.append([back_button(b"admin_audit_0", language=language)])
        await event.respond(
            t(language, "admin.audit.filter_result", count=result["total"]), buttons=buttons
        )
        return True

    config_flow = state.get("admin_subscription_config_flow")
    if config_flow:
        if not DataManager.is_admin(user_id):
            _clear_admin_input_state(user_id)
            return True
        if time.time() - float(config_flow.get("started_at", 0)) > _ADMIN_ACTION_TTL_SECONDS:
            _clear_admin_input_state(user_id)
            await event.respond(
                t(language, "admin.config.expired_notice"),
                buttons=[[back_button(b"admin_subscription_config", language=language)]],
            )
            return True
        fields = _SUBSCRIPTION_CONFIG_FIELDS[config_flow["target"]]
        if config_flow.get("stage") != "input" or config_flow["index"] >= len(fields):
            _clear_admin_input_state(user_id)
            await event.respond(
                t(language, "admin.config.state_invalid_notice"),
                buttons=[[back_button(b"admin_subscription_config", language=language)]],
            )
            return True
        field, _label, value_type, _example = fields[config_flow["index"]]
        try:
            parsed = _parse_config_value(event.text, value_type, language)
        except ValueError as error:
            await event.respond(
                _config_flow_prompt(config_flow, str(error), language),
                buttons=[[back_button(b"admin_subscription_config", language=language)]],
                parse_mode="md",
            )
            return True
        config_flow = copy.deepcopy(config_flow)
        config_flow["values"][field] = parsed
        config_flow["index"] += 1
        if config_flow["index"] < len(fields):
            set_state(user_id, admin_subscription_config_flow=config_flow)
            await event.respond(
                _config_flow_prompt(config_flow, language=language),
                buttons=[[back_button(b"admin_subscription_config", language=language)]],
                parse_mode="md",
            )
            return True
        config_flow["stage"] = "preview"
        set_state(user_id, admin_subscription_config_flow=config_flow)
        await event.respond(
            _config_flow_preview(config_flow, language),
            buttons=[
                [
                    Button.inline(t(language, "admin.common.save"), b"admin_subscription_config_confirm"),
                    Button.inline(t(language, "admin.common.cancel"), b"admin_subscription_config"),
                ],
                [Button.inline(
                    t(language, "admin.config.reenter"),
                    f"admin_subscription_config_edit_{config_flow['target']}".encode(),
                )],
            ],
        )
        return True

    if state.get("admin_user_subscription_input"):
        target_user_id = int(state["target_user_id"])
        plan_id = str(state["plan_id"])
        user_back = state.get("admin_user_back", "admin_panel")
        parts = (event.text or "").split()
        try:
            if len(parts) not in {1, 2}:
                raise ValueError(t(language, "admin.subscription.error.days_or_plus_quota"))
            days = int(parts[0])
            if days <= 0:
                raise ValueError(t(language, "admin.subscription.error.days_positive"))
            try:
                datetime.now() + timedelta(days=days)
            except (OverflowError, ValueError):
                raise ValueError(t(language, "admin.subscription.error.days_range"))
            if plan_id != "plus" and len(parts) != 1:
                raise ValueError(t(
                    language, "admin.subscription.error.quota_not_allowed",
                    plan=plan_id.upper(),
                ))
            quota = int(parts[1]) if plan_id == "plus" and len(parts) == 2 else None
            quote = DataManager.quote_subscription(plan_id, quota)
        except (TypeError, ValueError) as error:
            await event.respond(
                t(language, "admin.subscription.retry_input", error=error),
                buttons=[[back_button(
                    f"admin_user_detail_{target_user_id}".encode(), language=language
                )]],
            )
            return True
        _clear_admin_input_state(user_id)
        before = DataManager.get_subscription(target_user_id, include_inactive=True)
        pending = _queue_admin_action(
            user_id, "subscription.grant", target_user_id,
            {"plan_id": plan_id, "days": days, "quota": quote["quota"]},
            before,
        )
        set_state(user_id, admin_user_back=user_back)
        await event.respond(
            _grant_confirmation_text(target_user_id, quote, days, before, language),
            buttons=_confirmation_buttons(pending, language),
        )
        return True

    return False


def _audit_result(
    audit_id, admin_id, action, result, target_type="system", target_id=None,
    before=None, after=None, metadata=None, error=None,
):
    return AdminAuditLog.record_result(
        audit_id, result, before=before, after=after, error=error,
        admin_id=admin_id, action=action, target_type=target_type,
        target_id=target_id, metadata=metadata,
    )


def _queue_admin_action(admin_id, action, target_user_id, params, before, audit_id=None):
    previous = _pending_admin_actions.pop(int(admin_id), None)
    if previous:
        _audit_result(
            previous["audit_id"], admin_id, previous["audit_action"], "cancelled",
            previous["target_type"], previous["target_user_id"],
            before=previous["before"], error="replaced_by_new_action",
        )
    token = secrets.token_hex(6)
    audit_action = {
        "subscription.grant": "subscription.grant",
        "subscription.delete": "subscription.delete",
        "accounts.suspend": "accounts.suspend",
    }[action]
    target_type = "user"
    audit_id = audit_id or AdminAuditLog.record_attempt(
        admin_id, audit_action, target_type, target_user_id,
        metadata={"confirmation_required": True},
    )
    pending = {
        "token": token,
        "admin_id": int(admin_id),
        "action": action,
        "audit_action": audit_action,
        "audit_id": audit_id,
        "target_type": target_type,
        "target_user_id": int(target_user_id),
        "params": copy.deepcopy(params),
        "before": copy.deepcopy(before),
        "created_at": time.time(),
    }
    _pending_admin_actions[int(admin_id)] = pending
    return pending


def _confirmation_buttons(pending, language="zh"):
    token = pending["token"]
    return [
        [
            Button.inline(t(language, "admin.common.confirm"), f"admin_action_confirm_{token}".encode()),
            Button.inline(t(language, "admin.common.cancel"), f"admin_action_cancel_{token}".encode()),
        ],
        [back_button(
            f"admin_user_detail_{pending['target_user_id']}".encode(), language=language
        )],
    ]


def _grant_confirmation_text(target_user_id, quote, days, before, language="zh"):
    before_text = t(language, "admin.subscription.no_subscription")
    if before:
        before_text = t(
            language, "admin.subscription.current",
            plan=str(before.get("plan_id") or "-").upper(),
            expires_at=str(before.get("expires_at") or "-")[:19],
        )
    quota_text = (
        t(language, "admin.common.unlimited")
        if quote["quota"] is None
        else t(language, "admin.common.seats", count=quote["quota"])
    )
    return t(
        language, "admin.subscription.grant_confirm", user_id=target_user_id,
        current=before_text, plan=quote["plan_name"], days=days, quota=quota_text,
    )


def _take_admin_action(admin_id, token):
    pending = _pending_admin_actions.get(int(admin_id))
    if not pending or pending.get("token") != token:
        return None, "missing"
    _pending_admin_actions.pop(int(admin_id), None)
    if time.time() - float(pending["created_at"]) > _ADMIN_ACTION_TTL_SECONDS:
        _audit_result(
            pending["audit_id"], admin_id, pending["audit_action"], "cancelled",
            pending["target_type"], pending["target_user_id"],
            before=pending["before"], error="confirmation_expired",
        )
        return None, "expired"
    return pending, None


def _format_timestamp(value, language="zh"):
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return t(language, "admin.common.unknown")


def _order_status_text(order, language="zh"):
    if order.get("legacy_read_only") or order.get("legacy_origin") == "vip_purchase":
        return t(language, "admin.order.status.legacy")
    if order.get("processed"):
        return t(language, "admin.order.status.completed")
    if order.get("needs_manual_review") or order.get("status") == "paid":
        return t(language, "admin.order.status.review")
    if order.get("status") == "pending":
        return t(language, "admin.order.status.pending")
    if order.get("status") == "expired":
        return t(language, "admin.order.status.expired")
    if order.get("status") == "cancelled":
        return t(language, "admin.order.status.cancelled")
    return str(order.get("status") or t(language, "admin.common.unknown"))


def _known_user_ids(payment_system: PaymentSystem):
    ids = set(DataManager.get_all_user_ids())
    ids.update(int(user_id) for user_id in getattr(config, "ADMIN_IDS", []))
    ids.update(int(user_id) for user_id in user_accounts if str(user_id).isdigit())
    for user_id, _profile in UserProfileCache.iter_profiles():
        ids.add(user_id)
    for order in payment_system.pending_orders.values():
        try:
            ids.add(int(order.get("user_id")))
        except (AttributeError, TypeError, ValueError):
            pass
    return ids


def _normalized_phone(value):
    return "".join(character for character in str(value) if character.isdigit())


def _resumable_account_count(user_id, subscription, runtime_accounts):
    """Count sessions that the existing resume operation can actually start."""
    if not AccountManager.check_access(user_id):
        return 0
    subscription = subscription or {}
    hosted = AccountManager.hosted_account_phones(user_id)
    runtime = {
        digits for phone in runtime_accounts
        if (digits := _normalized_phone(phone))
    }
    selected = [
        digits for phone in subscription.get("selected_accounts") or []
        if (digits := _normalized_phone(phone)) in hosted
    ]
    quota = subscription.get("quota")
    if quota is not None:
        if subscription.get("selection_required"):
            return 0
        selected = selected[:int(quota)]
        if not selected:
            return 0
    elif not selected:
        selected = list(hosted)
    return len(set(selected) - runtime)


async def _search_admin_users(bot, payment_system: PaymentSystem, query: str):
    query = str(query or "").strip()
    known_ids = _known_user_ids(payment_system)
    profiles = dict(UserProfileCache.iter_profiles())
    phone_owners = {}
    for user_id, accounts in user_accounts.items():
        for phone in (accounts or {}):
            digits = _normalized_phone(phone)
            if digits:
                phone_owners.setdefault(digits, set()).add(int(user_id))

    lowered = query.casefold()
    username_query = lowered.lstrip("@")
    digits_query = _normalized_phone(query) if re.fullmatch(r"[\d+()\-\s]+", query) else ""
    matches = {}

    def add(user_id, rank, match_type):
        if user_id not in known_ids:
            return
        existing = matches.get(user_id)
        if existing is None or rank < existing[0]:
            matches[user_id] = (rank, match_type)

    if query.isdigit():
        add(int(query), 0, "user_id")
    if digits_query:
        for user_id in phone_owners.get(digits_query, set()):
            add(user_id, 2, "phone")

    for user_id, profile in profiles.items():
        username = str(profile.get("username") or "").casefold()
        display_name = str(profile.get("display_name") or "").casefold()
        if query.startswith("@"):
            if username == username_query:
                add(user_id, 1, "username")
        elif lowered:
            if username == username_query:
                add(user_id, 1, "username")
            elif username.startswith(username_query) or display_name.startswith(lowered):
                add(user_id, 3, "profile_prefix")
            elif username_query in username or lowered in display_name:
                add(user_id, 4, "profile_contains")

    if query.startswith("@") and not matches:
        try:
            entity = await bot.get_entity(query)
            entity_id = int(getattr(entity, "id", 0))
            if entity_id in known_ids:
                UserProfileCache.set_entity(entity)
                profiles[entity_id] = UserProfileCache.get_profile(entity_id) or {}
                add(entity_id, 1, "username")
        except Exception:
            pass

    results = []
    for user_id, (rank, match_type) in matches.items():
        profile = profiles.get(user_id) or UserProfileCache.get_profile(user_id) or {}
        results.append({
            "user_id": user_id,
            "rank": rank,
            "match_type": match_type,
            "display_name": profile.get("display_name"),
            "username": profile.get("username"),
        })
    results.sort(key=lambda item: (item["rank"], item["user_id"]))
    return results[:25]


async def setup_admin_handlers(bot: TelegramClient, payment_system: PaymentSystem = None):
    """Register admin panel, subscription management, pricing, and reminder callbacks."""
    def _format_amounts(amounts):
        if not amounts:
            return "0"
        return " · ".join(f"{amount} {coin}" for coin, amount in amounts.items())

    def _report_text(days, language):
        report = payment_system.get_admin_report(days) if payment_system else {
            "amounts": {}, "new_paid_users": 0, "start_time": time.time(), "end_time": time.time()
        }
        return t(
            language, "admin.report.text",
            title=t(language, f"admin.report.title.{days}"),
            revenue=_format_amounts(report["amounts"]),
            new_paid_users=report["new_paid_users"],
            start_time=_format_timestamp(report["start_time"], language),
            end_time=_format_timestamp(report["end_time"], language),
        )

    async def _render_admin_panel(event):
        """渲染管理面板，并结束未完成的管理员输入流程。"""
        user_id = event.sender_id
        language = _language(user_id)

        if not DataManager.is_admin(user_id):
            await event.answer(t(language, "admin.access_denied"), alert=True)
            return False

        _clear_admin_state(user_id)

        # ====== 系统统计（全局）======
        # 已加载账户：系统内已加载的全部账户（跨所有用户）
        total_accounts = sum(len(accounts) for accounts in user_accounts.values())
        # 已开启保护：anti_login 开启的账户数（跨所有用户）
        protected_accounts = sum(
            1 for accounts in user_accounts.values()
            for acc in (accounts or {}).values()
            if acc.get("anti_login")
        )

        # ====== 订阅概览 ======
        vip_users = DataManager.get_all_subscription_users()
        total_vips = len(vip_users)
        review_orders = (
            payment_system.list_admin_orders("review", page_size=1)["total"]
            if payment_system else 0
        )
        today_report = payment_system.get_admin_report(1) if payment_system else {
            "amounts": {}, "new_paid_users": 0
        }

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        text = t(
            language, "admin.panel.text", total_accounts=total_accounts,
            protected_accounts=protected_accounts, total_vips=total_vips,
            review_orders=review_orders,
            revenue=_format_amounts(today_report["amounts"]),
            new_paid_users=today_report["new_paid_users"], updated_at=now_str,
        )

        buttons = [
            [Button.inline(t(language, "admin.panel.orders"), b"admin_orders_review_0", icon=5877485980901971030), Button.inline(t(language, "admin.panel.user_search"), b"admin_user_search", icon=5874960879434338403)],
            [Button.inline(t(language, "admin.panel.grant"), b"admin_subscription_grant_help", icon=5775937998948404844), Button.inline(t(language, "admin.panel.delete"), b"admin_subscription_delete_help", icon=5879896690210639947)],
            [Button.inline(t(language, "admin.panel.subscribers"), b"admin_list_vip", icon=5960551395730919906), Button.inline(t(language, "admin.panel.config"), b"admin_subscription_config", icon=5879841310902324730)],
            [Button.inline(t(language, "admin.panel.report"), b"admin_report_1", icon=5994378914636500516), Button.inline(t(language, "admin.panel.audit"), b"admin_audit_0", icon=5877597667231534929)],
            [Button.inline(t(language, "admin.panel.reminders"), b"admin_reminder_settings", icon=5909201569898827582), Button.inline(t(language, "admin.panel.refresh"), b"admin_panel", icon=5877410604225924969)],
            [back_button(b"back_to_main", language=language)]
        ]

        try:
            await event.answer()
        except Exception:
            pass

        # 优先 edit（同一条消息刷新），仅普通编辑失败时 respond。
        await edit_or_respond(event, text, buttons=buttons, parse_mode="md")

        return True

    @bot.on(events.CallbackQuery(pattern=rb"admin_report_(1|7|30)"))
    async def admin_report(event):
        if not await require_admin(event, alert=True):
            return
        _clear_admin_input_state(event.sender_id)
        language = _language(event.sender_id)
        days = int(event.data.decode().rsplit("_", 1)[1])
        await event.answer()
        await safe_edit(
            event,
            _report_text(days, language),
            buttons=[
                [
                    Button.inline(t(language, "admin.report.tab.1"), b"admin_report_1"),
                    Button.inline(t(language, "admin.report.tab.7"), b"admin_report_7"),
                    Button.inline(t(language, "admin.report.tab.30"), b"admin_report_30"),
                ],
                [back_button(b"admin_panel", language=language)],
            ],
        )

    async def _render_order_list(event, category="review", page=0, query="", answer=True):
        _clear_admin_input_state(event.sender_id)
        language = _language(event.sender_id)
        if not payment_system:
            if answer:
                await event.answer(t(language, "admin.common.payment_unavailable"), alert=True)
            return
        result = payment_system.list_admin_orders(category, query=query, page=page, page_size=20)
        set_state(
            event.sender_id,
            admin_order_back=(
                f"admin_order_search_page_{page}" if query
                else f"admin_orders_{category}_{page}"
            ),
            admin_order_query=query,
        )
        audit_id = AdminAuditLog.record_attempt(
            event.sender_id, "order.list", "order", metadata={"category": category}
        )
        _audit_result(
            audit_id, event.sender_id, "order.list", "success", "order",
            metadata={"category": category, "result_count": result["total"]},
        )
        buttons = [
            [
                Button.inline(t(language, "admin.order.tab.review"), b"admin_orders_review_0"),
                Button.inline(t(language, "admin.order.tab.active"), b"admin_orders_active_0"),
                Button.inline(t(language, "admin.order.tab.completed"), b"admin_orders_completed_0"),
            ],
            [
                Button.inline(t(language, "admin.order.tab.closed"), b"admin_orders_closed_0"),
                Button.inline(t(language, "admin.order.tab.all"), b"admin_orders_all_0"),
                Button.inline(t(language, "admin.order.search_button"), b"admin_order_search"),
            ],
        ]
        for item in result["items"]:
            order_id = item["order_id"]
            label = (
                f"{_order_status_text(item, language)} · {order_id} · "
                f"{item.get('amount', '?')} {item.get('coin', '')}"
            )
            callback = f"admin_order_detail_{order_id}".encode()
            if len(callback) <= 64:
                buttons.append([Button.inline(label[:58], callback)])
        prefix = "admin_order_search_page" if query else f"admin_orders_{category}"
        nav = pagination_buttons(prefix, result["page"], result["max_page"], language)
        if nav:
            buttons.append(nav)
        buttons.append([back_button(b"admin_panel", language=language)])
        if answer:
            await event.answer()
        suffix = t(language, "admin.order.search_suffix") if query else ""
        await edit_or_respond(
            event,
            t(
                language, "admin.order.list",
                title=t(language, f"admin.order.category.{category}"),
                total=result["total"], page=result["page"] + 1,
                pages=result["max_page"] + 1, suffix=suffix,
            ),
            buttons=buttons,
        )

    async def _render_order_detail(event, order_id, answer=True):
        _clear_admin_input_state(event.sender_id)
        language = _language(event.sender_id)
        if not payment_system:
            return
        order = payment_system.get_order_snapshot(order_id)
        audit_id = AdminAuditLog.record_attempt(event.sender_id, "order.detail", "order", order_id)
        if not order:
            _audit_result(audit_id, event.sender_id, "order.detail", "failed", "order", order_id, error="not_found")
            if answer:
                await event.answer(t(language, "admin.order.not_found"), alert=True)
            return
        _audit_result(audit_id, event.sender_id, "order.detail", "success", "order", order_id)
        unknown = t(language, "admin.common.unknown")
        lines = [t(
            language, "admin.order.detail", order_id=order_id,
            status=_order_status_text(order, language),
            user_id=order.get("user_id", unknown), order_type=order.get("type", unknown),
            amount=order.get("amount", unknown), coin=order.get("coin", ""),
            plan=str(order.get("plan_id", "-")).upper(),
            days=order.get("period_days", "-"),
            created=_format_timestamp(order.get("created_time"), language),
            verified=_format_timestamp(order.get("verified_time"), language),
            fulfilled=_format_timestamp(order.get("fulfilled_time"), language),
        )]
        legacy_read_only = order.get("legacy_origin") == "vip_purchase"
        if legacy_read_only:
            lines.extend(["", t(language, "admin.order.legacy_notice")])
        reason = order.get("manual_review_reason") or order.get("payment_validation_error")
        if reason:
            lines.extend(["", t(
                language, "admin.order.reason", reason=_localized_reason(language, reason)
            )])
        retry = payment_system.get_order_retry_snapshot(order_id)
        if retry:
            lines.append(t(language, "admin.order.retry_failures", count=int(retry.get("failures", 0))))
            lines.append(t(language, "admin.order.next_check", time=_format_timestamp(retry.get("next_check_at"), language)))
        buttons = []
        if not legacy_read_only and not order.get("processed") and order.get("status", "pending") == "pending":
            callback = f"admin_order_check_{order_id}".encode()
            if len(callback) <= 64:
                buttons.append([Button.inline(t(language, "admin.order.recheck"), callback)])
        if not legacy_read_only and not order.get("processed") and order.get("status") == "paid":
            callback = f"admin_order_retry_{order_id}".encode()
            confirm_callback = f"admin_order_retry_confirm_{order_id}".encode()
            if len(callback) <= 64 and len(confirm_callback) <= 64:
                buttons.append([Button.inline(t(language, "admin.order.retry"), callback)])
        back_target = get_state(event.sender_id).get(
            "admin_order_back", "admin_orders_review_0"
        )
        buttons.append([back_button(str(back_target).encode(), language=language)])
        if answer:
            await event.answer()
        await safe_edit(event, "\n".join(lines), buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=rb"admin_orders_(review|active|completed|closed|all)_\d+"))
    async def admin_orders(event):
        if not await require_admin(event, alert=True):
            return
        match = re.fullmatch(r"admin_orders_(review|active|completed|closed|all)_(\d+)", event.data.decode())
        await _render_order_list(event, match.group(1), int(match.group(2)))

    @bot.on(events.CallbackQuery(pattern=b"admin_order_search"))
    async def admin_order_search(event):
        if not await require_admin(event, alert=True):
            return
        set_state(event.sender_id, admin_order_search=True)
        language = _language(event.sender_id)
        await event.answer()
        await safe_edit(
            event,
            t(language, "admin.order.search_prompt"),
            buttons=[[back_button(b"admin_orders_all_0", language=language)]],
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_order_search_page_\d+"))
    async def admin_order_search_page(event):
        if not await require_admin(event, alert=True):
            return
        page = int(event.data.decode().rsplit("_", 1)[1])
        query = get_state(event.sender_id).get("admin_order_query", "")
        await _render_order_list(event, "all", page, query=query)

    @bot.on(events.CallbackQuery(pattern=rb"admin_order_detail_.+"))
    async def admin_order_detail(event):
        if not await require_admin(event, alert=True):
            return
        order_id = event.data.decode().removeprefix("admin_order_detail_")
        await _render_order_detail(event, order_id)

    @bot.on(events.CallbackQuery(pattern=rb"admin_order_check_.+"))
    async def admin_order_check(event):
        if not await require_admin(event, alert=True) or not payment_system:
            return
        order_id = event.data.decode().removeprefix("admin_order_check_")
        language = _language(event.sender_id)
        before = payment_system.get_order_snapshot(order_id)
        audit_id = AdminAuditLog.record_attempt(event.sender_id, "order.recheck", "order", order_id)
        await event.answer(t(language, "admin.order.checking"))
        result = await payment_system.check_order_status(order_id)
        audited = _audit_result(
            audit_id, event.sender_id, "order.recheck",
            "success" if result.get("success") else "failed", "order", order_id,
            before={"status": (before or {}).get("status"), "processed": (before or {}).get("processed")},
            after={
                "status": (payment_system.get_order_snapshot(order_id) or {}).get("status"),
                "processed": (payment_system.get_order_snapshot(order_id) or {}).get("processed"),
            },
            error=result.get("error"),
        )
        await _render_order_detail(event, order_id, answer=False)
        if not audited:
            await event.respond(t(language, "admin.order.check_audit_warning"))

    @bot.on(events.CallbackQuery(pattern=rb"^admin_order_retry_(?!confirm_).+$"))
    async def admin_order_retry_prompt(event):
        if not await require_admin(event, alert=True) or not payment_system:
            return
        order_id = event.data.decode().removeprefix("admin_order_retry_")
        language = _language(event.sender_id)
        order = payment_system.get_order_snapshot(order_id)
        if not order or order.get("processed") or order.get("status") != "paid":
            await event.answer(t(language, "admin.order.state_changed"), alert=True)
            return
        callback = f"admin_order_retry_confirm_{order_id}".encode()
        await event.answer()
        await safe_edit(
            event,
            t(language, "admin.order.retry_prompt", order_id=order_id, user_id=order.get("user_id")),
            buttons=[
                [Button.inline(t(language, "admin.order.confirm_retry"), callback)],
                [back_button(f"admin_order_detail_{order_id}".encode(), language=language)],
            ],
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_order_retry_confirm_.+"))
    async def admin_order_retry_confirm(event):
        if not await require_admin(event, alert=True) or not payment_system:
            return
        order_id = event.data.decode().removeprefix("admin_order_retry_confirm_")
        language = _language(event.sender_id)
        before = payment_system.get_order_snapshot(order_id)
        audit_id = AdminAuditLog.record_attempt(event.sender_id, "order.retry_fulfillment", "order", order_id)
        if not before or before.get("processed") or before.get("status") != "paid":
            _audit_result(
                audit_id, event.sender_id, "order.retry_fulfillment", "failed",
                "order", order_id, error="order_state_changed",
            )
            await event.answer(t(language, "admin.order.state_changed"), alert=True)
            return
        await event.answer(t(language, "admin.order.retrying"))
        success = await payment_system.process_paid_order(order_id)
        after = payment_system.get_order_snapshot(order_id) or {}
        audited = _audit_result(
            audit_id, event.sender_id, "order.retry_fulfillment",
            "success" if success else "failed", "order", order_id,
            before={"status": before.get("status"), "processed": before.get("processed")},
            after={"status": after.get("status"), "processed": after.get("processed")},
            error=None if success else after.get("manual_review_reason", "fulfillment_failed"),
        )
        await _render_order_detail(event, order_id, answer=False)
        if not audited:
            await event.respond(t(language, "admin.order.retry_audit_warning"))

    @bot.on(events.CallbackQuery(pattern=rb"admin_action_cancel_[0-9a-f]+"))
    async def admin_action_cancel(event):
        if not await require_admin(event, alert=True):
            return
        token = event.data.decode().removeprefix("admin_action_cancel_")
        language = _language(event.sender_id)
        pending, error = _take_admin_action(event.sender_id, token)
        if not pending:
            await event.answer(t(language, "admin.common.expired_or_processed"), alert=True)
            return
        _audit_result(
            pending["audit_id"], event.sender_id, pending["audit_action"], "cancelled",
            pending["target_type"], pending["target_user_id"],
            before=pending["before"], error="cancelled_by_admin",
        )
        await event.answer(t(language, "admin.common.cancelled"))
        await safe_edit(
            event, t(language, "admin.common.cancelled_text"),
            buttons=[[back_button(
                f"admin_user_detail_{pending['target_user_id']}".encode(), language=language
            )]],
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_action_confirm_[0-9a-f]+"))
    async def admin_action_confirm(event):
        if not await require_admin(event, alert=True):
            return
        language = _language(event.sender_id)
        token = event.data.decode().removeprefix("admin_action_confirm_")
        pending, error = _take_admin_action(event.sender_id, token)
        if not pending:
            await event.answer(
                t(language, "admin.common.confirm_expired")
                if error == "expired" else t(language, "admin.common.expired_or_processed"),
                alert=True,
            )
            return
        target_user_id = pending["target_user_id"]
        current = DataManager.get_subscription(target_user_id, include_inactive=True)
        current_account_keys = sorted(user_accounts.get(target_user_id, {}))
        expected_account_keys = pending["params"].get("account_keys")
        if (
            current != pending["before"]
            or (
                expected_account_keys is not None
                and current_account_keys != expected_account_keys
            )
        ):
            _audit_result(
                pending["audit_id"], event.sender_id, pending["audit_action"], "failed",
                "user", target_user_id, before=pending["before"], after=current,
                error="target_state_changed",
            )
            await event.answer(t(language, "admin.common.state_changed"), alert=True)
            return
        await event.answer(t(language, "admin.common.running"))
        try:
            if pending["action"] == "subscription.grant":
                params = pending["params"]
                success = DataManager.grant_subscription(
                    target_user_id, params["plan_id"], params["days"], params["quota"]
                )
                after = DataManager.get_subscription(target_user_id, include_inactive=True)
                if not success:
                    raise RuntimeError("grant_conflict_or_save_failed")
                metadata = copy.deepcopy(params)
                message = t(language, "admin.subscription.changed")
            elif pending["action"] == "subscription.delete":
                try:
                    suspended = await AccountManager.suspend_user_accounts(target_user_id)
                except Exception:
                    await AccountManager.resume_selected_accounts(target_user_id)
                    raise
                if not DataManager.delete_subscription(target_user_id):
                    await AccountManager.resume_selected_accounts(target_user_id)
                    raise RuntimeError("delete_save_failed")
                after = None
                metadata = {"suspended_accounts": suspended}
                message = t(language, "admin.subscription.deleted", count=suspended)
            elif pending["action"] == "accounts.suspend":
                try:
                    suspended = await AccountManager.suspend_user_accounts(target_user_id)
                except Exception:
                    await AccountManager.resume_selected_accounts(target_user_id)
                    raise
                after = current
                metadata = {"suspended_accounts": suspended}
                message = t(language, "admin.subscription.accounts_suspended", count=suspended)
            else:
                raise RuntimeError("unknown_action")
        except Exception as exc:
            _audit_result(
                pending["audit_id"], event.sender_id, pending["audit_action"], "failed",
                "user", target_user_id, before=pending["before"], error=str(exc),
            )
            logger.exception("管理员确认操作失败: %s", pending["action"])
            await safe_edit(
                event, t(language, "admin.common.failed_safe"),
                buttons=[[back_button(
                    f"admin_user_detail_{target_user_id}".encode(), language=language
                )]],
            )
            return
        audited = _audit_result(
            pending["audit_id"], event.sender_id, pending["audit_action"], "success",
            "user", target_user_id, before=pending["before"], after=after,
            metadata=metadata,
        )
        if not audited:
            message += "\n" + t(language, "admin.common.audit_warning")
        await safe_edit(
            event, message,
            buttons=[[back_button(
                f"admin_user_detail_{target_user_id}".encode(), language=language
            )]],
        )

    @bot.on(events.CallbackQuery(pattern=b"admin_user_search"))
    async def admin_user_search(event):
        if not await require_admin(event, alert=True):
            return
        language = _language(event.sender_id)
        set_state(event.sender_id, admin_user_search=True)
        await event.answer()
        await safe_edit(
            event,
            t(language, "admin.user.search_prompt"),
            buttons=[[back_button(b"admin_panel", language=language)]],
        )

    @bot.on(events.CallbackQuery(pattern=b"admin_user_search_results"))
    async def admin_user_search_results(event):
        if not await require_admin(event, alert=True):
            return
        language = _language(event.sender_id)
        results = get_state(event.sender_id).get("admin_user_results") or []
        if not results:
            await event.answer(t(language, "admin.user.search_expired"), alert=True)
            return
        buttons = []
        for item in results:
            label = item.get("display_name") or (
                f"@{item['username']}" if item.get("username")
                else t(language, "admin.user.fallback_name", user_id=item["user_id"])
            )
            buttons.append([Button.inline(
                f"{label} · {item['user_id']}"[:58],
                f"admin_user_detail_{item['user_id']}".encode(),
            )])
        buttons.append([back_button(b"admin_panel", language=language)])
        await event.answer()
        await safe_edit(
            event, t(language, "admin.user.search_title", count=len(results), warning=""),
            buttons=buttons,
        )

    async def _render_user_detail(event, target_user_id, answer=True):
        _clear_admin_input_state(event.sender_id)
        language = _language(event.sender_id)
        audit_id = AdminAuditLog.record_attempt(event.sender_id, "user.detail", "user", target_user_id)
        await _get_user_display_name(bot, target_user_id)
        profile = UserProfileCache.get_profile(target_user_id) or {}
        subscription = DataManager.get_subscription(target_user_id, include_inactive=True) or {}
        accounts = user_accounts.get(target_user_id, {}) or {}
        quota = subscription.get("quota", 0)
        active = bool(subscription.get("active"))
        role_key = (
            "admin.user.role.admin" if DataManager.is_admin(target_user_id)
            else "admin.user.role.subscriber" if active else "admin.user.role.regular"
        )
        quota_text = (
            t(language, "admin.common.unlimited") if quota is None else str(quota)
        )
        lines = [t(
            language, "admin.user.detail",
            name=profile.get("display_name") or t(language, "admin.common.unknown"),
            username="@" + profile["username"] if profile.get("username") else t(language, "admin.common.none"),
            user_id=target_user_id, role=t(language, role_key),
            plan=subscription.get("plan_name") or subscription.get("plan_id") or t(language, "admin.common.none"),
            expires_at=str(subscription.get("expires_at") or "-")[:19],
            quota=quota_text, used=len(accounts),
        )]
        scheduled = subscription.get("scheduled")
        if scheduled:
            scheduled_quota = (
                t(language, "admin.common.unlimited")
                if scheduled.get("quota") is None
                else t(language, "admin.common.seats", count=scheduled.get("quota"))
            )
            lines.append(t(
                language, "admin.user.scheduled",
                plan=str(scheduled.get("plan_id", "")).upper(), quota=scheduled_quota,
            ))
        lines.extend(["", t(language, "admin.user.hosted_accounts")])
        if accounts:
            for phone, account in list(accounts.items())[:40]:
                health = _localized_code(
                    language, "admin.account.status",
                    account.get("health_status") or account.get("runtime_status"), "unknown",
                )
                status = "🟢" if account.get("anti_login") else "🔴"
                lines.append(f"{status} {account.get('display_phone', phone)} · {health}")
            if len(accounts) > 40:
                lines.append(t(language, "admin.user.more_accounts", count=len(accounts) - 40))
        else:
            lines.append(t(language, "admin.user.no_accounts"))
        orders = payment_system.get_user_order_summaries(target_user_id, 5) if payment_system else {"total": 0, "items": []}
        lines.extend(["", t(language, "admin.user.order_count", count=orders["total"])])
        for order in orders["items"]:
            lines.append(
                f"• {_order_status_text(order, language)} · {order['order_id']} · "
                f"{order.get('amount', '?')} {order.get('coin', '')}"
            )
        audited = _audit_result(
            audit_id, event.sender_id, "user.detail", "success", "user", target_user_id,
            metadata={"hosting_count": len(accounts), "order_count": orders["total"]},
        )
        if not audited:
            lines.extend(["", t(language, "admin.common.query_audit_warning")])
        buttons = []
        primary_buttons = []
        is_admin_target = DataManager.is_admin(target_user_id)
        if not is_admin_target:
            if subscription:
                primary_buttons.append(Button.inline(
                    t(language, "admin.user.manage_subscription"),
                    f"admin_user_subscription_{target_user_id}".encode(),
                ))
            else:
                primary_buttons.append(Button.inline(
                    t(language, "admin.user.grant_subscription"),
                    f"admin_user_sub_{target_user_id}".encode(),
                ))
        if orders["total"]:
            primary_buttons.append(Button.inline(
                t(language, "admin.user.orders_button", count=orders["total"]),
                f"admin_user_orders_{target_user_id}_0".encode(),
            ))
        if primary_buttons:
            buttons.append(primary_buttons)

        resumable_count = _resumable_account_count(
            target_user_id, subscription, accounts
        )
        if accounts or resumable_count:
            buttons.append([
                Button.inline(
                    t(language, "admin.user.account_actions"),
                    f"admin_user_accounts_{target_user_id}".encode(),
                )
            ])
        user_back = get_state(event.sender_id).get("admin_user_back", "admin_panel")
        buttons.append([back_button(str(user_back).encode(), language=language)])
        if answer:
            await event.answer()
        await safe_edit(event, "\n".join(lines), buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=rb"admin_user_detail_\d+"))
    async def admin_user_detail(event):
        if not await require_admin(event, alert=True):
            return
        target_user_id = int(event.data.decode().removeprefix("admin_user_detail_"))
        await _render_user_detail(event, target_user_id)

    @bot.on(events.CallbackQuery(pattern=rb"admin_user_subscription_\d+"))
    async def admin_user_subscription(event):
        if not await require_admin(event, alert=True):
            return
        target_user_id = int(event.data.decode().removeprefix("admin_user_subscription_"))
        language = _language(event.sender_id)
        if DataManager.is_admin(target_user_id):
            await event.answer(t(language, "admin.user.subscription_admin"), alert=True)
            return
        subscription = DataManager.get_subscription(target_user_id, include_inactive=True)
        if not subscription:
            await admin_user_sub(event, target_user_id)
            return
        status = t(
            language,
            "admin.user.subscription_active" if subscription.get("active")
            else "admin.user.subscription_expired",
        )
        plan = str(subscription.get("plan_name") or subscription.get("plan_id") or "-").upper()
        await event.answer()
        await safe_edit(
            event,
            t(
                language, "admin.user.subscription_text", user_id=target_user_id,
                status=status, plan=plan,
                expires_at=str(subscription.get("expires_at") or "-")[:19],
            ),
            buttons=[
                [Button.inline(
                    t(language, "admin.user.extend_subscription"),
                    f"admin_user_sub_{target_user_id}".encode(),
                )],
                [Button.inline(
                    t(language, "admin.user.delete_subscription"),
                    f"admin_user_delete_{target_user_id}".encode(),
                )],
                [back_button(f"admin_user_detail_{target_user_id}".encode(), language=language)],
            ],
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_user_accounts_\d+"))
    async def admin_user_accounts(event):
        if not await require_admin(event, alert=True):
            return
        target_user_id = int(event.data.decode().removeprefix("admin_user_accounts_"))
        language = _language(event.sender_id)
        accounts = user_accounts.get(target_user_id, {}) or {}
        subscription = DataManager.get_subscription(target_user_id, include_inactive=True)
        resumable_count = _resumable_account_count(
            target_user_id, subscription, accounts
        )
        buttons = []
        if accounts:
            buttons.append([Button.inline(
                t(language, "admin.user.reload"),
                f"admin_user_reload_{target_user_id}".encode(),
            )])
        if resumable_count:
            buttons.append([Button.inline(
                t(language, "admin.user.resume"),
                f"admin_user_resume_{target_user_id}".encode(),
            )])
        if accounts:
            buttons.append([Button.inline(
                t(language, "admin.user.suspend"),
                f"admin_user_suspend_{target_user_id}".encode(),
            )])
        buttons.append([back_button(f"admin_user_detail_{target_user_id}".encode(), language=language)])
        await event.answer()
        await safe_edit(
            event,
            t(
                language, "admin.user.accounts_text", user_id=target_user_id,
                running=len(accounts), resumable=resumable_count,
            ),
            buttons=buttons,
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_user_sub_\d+"))
    async def admin_user_sub(event, target_user_id=None):
        if not await require_admin(event, alert=True):
            return
        if target_user_id is None:
            target_user_id = int(event.data.decode().removeprefix("admin_user_sub_"))
        language = _language(event.sender_id)
        await event.answer()
        await safe_edit(
            event,
            t(language, "admin.user.choose_plan", user_id=target_user_id),
            buttons=[
                [
                    Button.inline("GO", f"admin_user_plan_{target_user_id}_go".encode()),
                    Button.inline("PLUS", f"admin_user_plan_{target_user_id}_plus".encode()),
                    Button.inline("PRO", f"admin_user_plan_{target_user_id}_pro".encode()),
                ],
                [back_button(f"admin_user_detail_{target_user_id}".encode(), language=language)],
            ],
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_user_plan_\d+_(go|plus|pro)"))
    async def admin_user_plan(event):
        if not await require_admin(event, alert=True):
            return
        match = re.fullmatch(r"admin_user_plan_(\d+)_(go|plus|pro)", event.data.decode())
        target_user_id, plan_id = int(match.group(1)), match.group(2)
        language = _language(event.sender_id)
        set_state(
            event.sender_id, admin_user_subscription_input=True,
            target_user_id=target_user_id, plan_id=plan_id,
            admin_user_back=get_state(event.sender_id).get("admin_user_back", "admin_panel"),
        )
        prompt = t(language, "admin.user.days_prompt")
        if plan_id == "plus":
            prompt += t(language, "admin.user.plus_days_prompt")
        await event.answer()
        await safe_edit(
            event, prompt,
            buttons=[[back_button(
                f"admin_user_detail_{target_user_id}".encode(), language=language
            )]],
            parse_mode="md",
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_user_delete_\d+"))
    async def admin_user_delete(event):
        if not await require_admin(event, alert=True):
            return
        target_user_id = int(event.data.decode().removeprefix("admin_user_delete_"))
        language = _language(event.sender_id)
        before = DataManager.get_subscription(target_user_id, include_inactive=True)
        if not before:
            await event.answer(t(language, "admin.user.no_subscription"), alert=True)
            return
        pending = _queue_admin_action(
            event.sender_id, "subscription.delete", target_user_id,
            {"account_keys": sorted(user_accounts.get(target_user_id, {}))}, before
        )
        await event.answer()
        await safe_edit(
            event,
            t(
                language, "admin.user.delete_confirm", user_id=target_user_id,
                plan=str(before.get("plan_id") or "-").upper(),
                expires_at=str(before.get("expires_at") or "-")[:19],
            ),
            buttons=_confirmation_buttons(pending, language),
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_user_reload_\d+"))
    async def admin_user_reload(event):
        if not await require_admin(event, alert=True):
            return
        target_user_id = int(event.data.decode().removeprefix("admin_user_reload_"))
        language = _language(event.sender_id)
        audit_id = AdminAuditLog.record_attempt(
            event.sender_id, "accounts.reload", "user", target_user_id
        )
        await event.answer(t(language, "admin.user.reconnecting"))
        try:
            result = await AccountManager.reload_user_accounts_detail(
                target_user_id, source="manual_reload"
            )
        except Exception as error:
            _audit_result(
                audit_id, event.sender_id, "accounts.reload", "failed", "user",
                target_user_id, error=str(error),
            )
            logger.exception("管理员重连用户账户失败: %s", target_user_id)
            await safe_edit(
                event, t(language, "admin.user.reload_failed"),
                buttons=[[back_button(f"admin_user_detail_{target_user_id}".encode(), language=language)]],
            )
            return
        _audit_result(
            audit_id, event.sender_id, "accounts.reload", "success", "user",
            target_user_id, metadata=result,
        )
        await safe_edit(
            event,
            t(
                language, "admin.user.reload_done", total=result.get("total", 0),
                success=result.get("success", 0), failed=result.get("failed", 0),
            ),
            buttons=[[back_button(f"admin_user_detail_{target_user_id}".encode(), language=language)]],
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_user_suspend_\d+"))
    async def admin_user_suspend(event):
        if not await require_admin(event, alert=True):
            return
        target_user_id = int(event.data.decode().removeprefix("admin_user_suspend_"))
        language = _language(event.sender_id)
        before = DataManager.get_subscription(target_user_id, include_inactive=True)
        pending = _queue_admin_action(
            event.sender_id, "accounts.suspend", target_user_id,
            {"account_keys": sorted(user_accounts.get(target_user_id, {}))}, before
        )
        await event.answer()
        await safe_edit(
            event,
            t(
                language, "admin.user.suspend_confirm", user_id=target_user_id,
                count=len(user_accounts.get(target_user_id, {})),
            ),
            buttons=_confirmation_buttons(pending, language),
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_user_resume_\d+"))
    async def admin_user_resume(event):
        if not await require_admin(event, alert=True):
            return
        target_user_id = int(event.data.decode().removeprefix("admin_user_resume_"))
        language = _language(event.sender_id)
        audit_id = AdminAuditLog.record_attempt(
            event.sender_id, "accounts.resume", "user", target_user_id
        )
        await event.answer(t(language, "admin.user.resuming"))
        try:
            resumed = await AccountManager.resume_selected_accounts(target_user_id)
        except Exception as error:
            _audit_result(
                audit_id, event.sender_id, "accounts.resume", "failed", "user",
                target_user_id, error=str(error),
            )
            await safe_edit(
                event, t(language, "admin.user.resume_failed"),
                buttons=[[back_button(f"admin_user_detail_{target_user_id}".encode(), language=language)]],
            )
            return
        _audit_result(
            audit_id, event.sender_id, "accounts.resume", "success", "user",
            target_user_id, metadata={"resumed_accounts": resumed},
        )
        await safe_edit(
            event, t(language, "admin.user.resume_done", count=resumed),
            buttons=[[back_button(f"admin_user_detail_{target_user_id}".encode(), language=language)]],
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_user_orders_\d+_\d+"))
    async def admin_user_orders(event):
        if not await require_admin(event, alert=True) or not payment_system:
            return
        match = re.fullmatch(r"admin_user_orders_(\d+)_(\d+)", event.data.decode())
        target_user_id, page = int(match.group(1)), int(match.group(2))
        language = _language(event.sender_id)
        result = payment_system.list_admin_orders(
            "all", query=str(target_user_id), page=page, page_size=20
        )
        set_state(
            event.sender_id,
            admin_order_back=f"admin_user_orders_{target_user_id}_{page}",
            admin_user_back=get_state(event.sender_id).get("admin_user_back", "admin_panel"),
        )
        buttons = []
        for item in result["items"]:
            callback = f"admin_order_detail_{item['order_id']}".encode()
            if len(callback) <= 64:
                buttons.append([Button.inline(
                    f"{_order_status_text(item, language)} · {item['order_id']}"[:58], callback
                )])
        nav = pagination_buttons(
            f"admin_user_orders_{target_user_id}", result["page"], result["max_page"], language
        )
        if nav:
            buttons.append(nav)
        buttons.append([back_button(f"admin_user_detail_{target_user_id}".encode(), language=language)])
        await event.answer()
        await safe_edit(
            event,
            t(
                language, "admin.user.orders_text", user_id=target_user_id,
                total=result["total"], page=result["page"] + 1,
                pages=result["max_page"] + 1,
            ),
            buttons=buttons,
        )

    async def _render_audit_list(event, page=0, answer=True):
        _clear_admin_input_state(event.sender_id)
        language = _language(event.sender_id)
        filters = dict(get_state(event.sender_id).get("admin_audit_filters") or {})
        filters["exclude_attempt"] = True
        result = AdminAuditLog.query(filters, page=page, page_size=25)
        buttons = []
        for item in result["items"]:
            icon = "✅" if item.get("result") == "success" else "❌"
            buttons.append([Button.inline(
                f"{icon} {_localized_code(language, 'admin.audit.action', item.get('action'))} · "
                f"{item.get('target_id') or '-'}"[:58],
                f"admin_audit_detail_{item['audit_id']}".encode(),
            )])
        nav = pagination_buttons("admin_audit", result["page"], result["max_page"], language)
        if nav:
            buttons.append(nav)
        buttons.extend([
            [Button.inline(t(language, "admin.audit.filter"), b"admin_audit_filter"), Button.inline(t(language, "admin.audit.clear"), b"admin_audit_clear")],
            [Button.inline(t(language, "admin.audit.download"), b"admin_audit_download")],
            [back_button(b"admin_panel", language=language)],
        ])
        if answer:
            await event.answer()
        filter_text = t(language, "admin.audit.filtered") if len(filters) > 1 else ""
        await safe_edit(
            event,
            t(
                language, "admin.audit.title", filter=filter_text, total=result["total"],
                page=result["page"] + 1, pages=result["max_page"] + 1,
            ),
            buttons=buttons,
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_audit_\d+"))
    async def admin_audit(event):
        if not await require_admin(event, alert=True):
            return
        language = _language(event.sender_id)
        page = int(event.data.decode().rsplit("_", 1)[1])
        await _render_audit_list(event, page)

    @bot.on(events.CallbackQuery(pattern=b"admin_audit_filter"))
    async def admin_audit_filter(event):
        if not await require_admin(event, alert=True):
            return
        language = _language(event.sender_id)
        set_state(event.sender_id, admin_audit_filter=True)
        await event.answer()
        await safe_edit(
            event,
            t(language, "admin.audit.filter_prompt"),
            buttons=[[back_button(b"admin_audit_0", language=language)]],
        )

    @bot.on(events.CallbackQuery(pattern=b"admin_audit_clear"))
    async def admin_audit_clear(event):
        if not await require_admin(event, alert=True):
            return
        clear_state(event.sender_id)
        await _render_audit_list(event, 0)

    @bot.on(events.CallbackQuery(pattern=rb"admin_audit_detail_[0-9a-f]+"))
    async def admin_audit_detail(event):
        if not await require_admin(event, alert=True):
            return
        audit_id = event.data.decode().removeprefix("admin_audit_detail_")
        language = _language(event.sender_id)
        entries = AdminAuditLog.get_by_audit_id(audit_id)
        if not entries:
            await event.answer(t(language, "admin.audit.not_found"), alert=True)
            return
        text = _format_audit_entries(entries, language)
        await event.answer()
        await safe_edit(
            event, text[:3900],
            buttons=[[back_button(b"admin_audit_0", language=language)]],
        )

    @bot.on(events.CallbackQuery(pattern=b"admin_audit_download"))
    async def admin_audit_download(event):
        if not await require_admin(event, alert=True):
            return
        language = _language(event.sender_id)
        audit_id = AdminAuditLog.record_attempt(event.sender_id, "audit.download", "audit")
        AdminAuditLog.prune()
        path = audit_file_path()
        if not os.path.exists(path):
            _audit_result(audit_id, event.sender_id, "audit.download", "failed", "audit", error="not_found")
            await event.answer(t(language, "admin.audit.no_logs"), alert=True)
            return
        await event.answer(t(language, "admin.audit.sending"))
        try:
            await bot.send_file(
                event.sender_id, path, caption=t(language, "admin.audit.caption")
            )
            audited = _audit_result(audit_id, event.sender_id, "audit.download", "success", "audit")
            if not audited:
                await event.respond(t(language, "admin.audit.sent_warning"))
        except Exception as error:
            _audit_result(audit_id, event.sender_id, "audit.download", "failed", "audit", error=str(error))
            await event.respond(t(language, "admin.audit.send_failed"))

    async def _render_subscription_config(event, *, answer=True, notice=None):
        _clear_admin_input_state(event.sender_id)
        language = _language(event.sender_id)
        catalog = DataManager.get_subscription_catalog()
        periods = DataManager.get_subscription_periods()
        go, plus, pro = catalog['go'], catalog['plus'], catalog['pro']
        if answer:
            await event.answer()
        await safe_edit(
            event,
            t(
                language, "admin.config.text",
                notice=f"{notice}\n\n" if notice else "",
                go_price=go["price"], go_quota=go["quota"],
                plus_price=plus["price"], plus_quota=plus["quota"],
                addon_price=plus["addon_unit_price"], min_addon=plus["min_addon"],
                pro_price=pro["price"],
                discount_90=periods[90]["discount_percent"],
                discount_180=periods[180]["discount_percent"],
                discount_365=periods[365]["discount_percent"],
            ),
            buttons=[
                [
                    Button.inline(t(language, "admin.config.edit_go"), b"admin_subscription_config_edit_go"),
                    Button.inline(t(language, "admin.config.edit_plus"), b"admin_subscription_config_edit_plus"),
                ],
                [
                    Button.inline(t(language, "admin.config.edit_pro"), b"admin_subscription_config_edit_pro"),
                    Button.inline(t(language, "admin.config.edit_discounts"), b"admin_subscription_config_edit_discounts"),
                ],
                [back_button(b"admin_panel", language=language)],
            ],
        )

    @bot.on(events.CallbackQuery(pattern=rb"^admin_subscription_config$"))
    async def admin_subscription_config(event):
        if not await require_admin(event, alert=True):
            return
        await _render_subscription_config(event)

    @bot.on(events.CallbackQuery(
        pattern=rb"^admin_subscription_config_edit_(go|plus|pro|discounts)$"
    ))
    async def admin_subscription_config_edit(event):
        if not await require_admin(event, alert=True):
            return
        language = _language(event.sender_id)
        target = event.data.decode().removeprefix("admin_subscription_config_edit_")
        _clear_admin_input_state(event.sender_id)
        before = (
            DataManager.get_subscription_periods()
            if target == "discounts"
            else DataManager.get_subscription_catalog()
        )
        flow = {
            "target": target,
            "before": copy.deepcopy(before),
            "values": {},
            "index": 0,
            "stage": "input",
            "started_at": time.time(),
        }
        set_state(event.sender_id, admin_subscription_config_flow=flow)
        await event.answer()
        await safe_edit(
            event,
            _config_flow_prompt(flow, language=language),
            buttons=[[back_button(b"admin_subscription_config", language=language)]],
            parse_mode="md",
        )

    @bot.on(events.CallbackQuery(pattern=rb"^admin_subscription_config_confirm$"))
    async def admin_subscription_config_confirm(event):
        if not await require_admin(event, alert=True):
            return
        language = _language(event.sender_id)
        flow = get_state(event.sender_id).get("admin_subscription_config_flow")
        if not flow or flow.get("stage") != "preview":
            await event.answer(t(language, "admin.config.invalid"), alert=True)
            await _render_subscription_config(event, answer=False)
            return
        if time.time() - float(flow.get("started_at", 0)) > _ADMIN_ACTION_TTL_SECONDS:
            await event.answer(t(language, "admin.config.expired"), alert=True)
            await _render_subscription_config(
                event, answer=False, notice=t(language, "admin.config.expired_notice")
            )
            return
        is_discounts = flow["target"] == "discounts"
        current = (
            DataManager.get_subscription_periods()
            if is_discounts
            else DataManager.get_subscription_catalog()
        )
        if current != flow["before"]:
            await event.answer(t(language, "admin.config.changed"), alert=True)
            await _render_subscription_config(
                event, answer=False, notice=t(language, "admin.config.changed_notice")
            )
            return
        candidate = _config_flow_candidate(flow)
        action = (
            "config.subscription_discounts_set"
            if is_discounts else "config.subscription_catalog_set"
        )
        target_type = "subscription_periods" if is_discounts else "subscription_catalog"
        audit_id = AdminAuditLog.record_attempt(
            event.sender_id, action, target_type,
            metadata={"section": flow["target"], "button_flow": True},
        )
        success = (
            DataManager.set_subscription_periods(candidate)
            if is_discounts else DataManager.set_subscription_catalog(candidate)
        )
        if not success:
            _audit_result(
                audit_id, event.sender_id, action, "failed", target_type,
                before=current, error="save_failed",
            )
            await event.answer(t(language, "admin.config.save_failed"), alert=True)
            await _render_subscription_config(
                event, answer=False, notice=t(language, "admin.config.save_failed_notice")
            )
            return
        after = (
            DataManager.get_subscription_periods()
            if is_discounts
            else DataManager.get_subscription_catalog()
        )
        audited = _audit_result(
            audit_id, event.sender_id, action, "success", target_type,
            before=current, after=after,
        )
        await event.answer(t(language, "admin.config.saved"))
        notice = t(language, "admin.config.saved_notice")
        if not audited:
            notice += t(language, "admin.config.audit_warning")
        await _render_subscription_config(event, answer=False, notice=notice)

    @bot.on(events.CallbackQuery(pattern=b"admin_panel"))
    async def admin_panel(event):
        await _render_admin_panel(event)

    @bot.on(events.CallbackQuery(pattern=b"admin_reminder_settings"))
    async def admin_reminder_settings(event):
        """Reminder settings hub."""
        user_id = event.sender_id
        language = _language(user_id)
        if not await require_admin(event, alert=True):
            return
        _clear_admin_input_state(user_id)
        await event.answer()
        await safe_edit(
            event,
            t(language, "admin.reminder.hub"),
            buttons=[
                [Button.inline(
                    t(language, "admin.reminder.expiry_button"),
                    b"admin_expiry_reminder_settings",
                )],
                [Button.inline(
                    t(language, "admin.reminder.login_unlock_button"),
                    b"admin_login_unlock_reminder_settings",
                )],
                [back_button(b"admin_panel", language=language)],
            ],
        )

    @bot.on(events.CallbackQuery(pattern=b"admin_expiry_reminder_settings"))
    async def admin_expiry_reminder_settings(event):
        user_id = event.sender_id
        language = _language(user_id)
        if not await require_admin(event, alert=True):
            return
        _clear_admin_input_state(user_id)
        current_days = DataManager.get_expiry_reminder_days()
        expiring_users = DataManager.get_expiring_subscription_users(current_days)
        message = t(
            language, "admin.reminder.text", days=current_days, count=len(expiring_users)
        )
        buttons = [
            [Button.inline(t(language, "admin.reminder.option", days=1), b"set_reminder_1"), Button.inline(t(language, "admin.reminder.option", days=3), b"set_reminder_3")],
            [Button.inline(t(language, "admin.reminder.option", days=7), b"set_reminder_7"), Button.inline(t(language, "admin.reminder.option", days=15), b"set_reminder_15")],
            [back_button(b"admin_reminder_settings", language=language)]
        ]
        await event.answer()
        await safe_edit(event, message, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=b"admin_login_unlock_reminder_settings"))
    async def admin_login_unlock_reminder_settings(event):
        user_id = event.sender_id
        language = _language(user_id)
        if not await require_admin(event, alert=True):
            return
        _clear_admin_input_state(user_id)
        schedule = DataManager.get_login_unlock_reminder_schedule()
        schedule_text = " / ".join(
            format_offset(value, language) for value in schedule["offsets_seconds"]
        )
        buttons = [
            [
                Button.inline(str(value), f"admin_login_unlock_count_{value}".encode())
                for value in range(1, 4)
            ],
            [
                Button.inline(str(value), f"admin_login_unlock_count_{value}".encode())
                for value in range(4, 6)
            ],
            [back_button(b"admin_reminder_settings", language=language)],
        ]
        await event.answer()
        await safe_edit(
            event,
            t(
                language,
                "admin.login_unlock.text",
                count=schedule["count"],
                schedule=schedule_text,
            ),
            buttons=buttons,
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_login_unlock_count_[1-5]"))
    async def admin_login_unlock_count(event):
        if not await require_admin(event, alert=True):
            return
        count = int(event.data.decode().rsplit("_", 1)[1])
        language = _language(event.sender_id)
        set_state(event.sender_id, admin_login_unlock_reminder_count=count)
        await event.answer()
        await safe_edit(
            event,
            t(language, "admin.login_unlock.prompt", count=count),
            buttons=[[back_button(b"admin_login_unlock_reminder_settings", language=language)]],
            parse_mode="md",
        )

    @bot.on(events.CallbackQuery(pattern=rb"set_reminder_\d+"))
    async def set_reminder_days(event):
        """设置提醒天数"""
        user_id = event.sender_id
        language = _language(user_id)
        
        if not await require_admin(event, alert=True):
            return
        
        days = int(event.data.decode().replace("set_reminder_", ""))
        before = DataManager.get_expiry_reminder_days()
        audit_id = AdminAuditLog.record_attempt(user_id, "config.expiry_reminder_set", "system_setting", "expiry_reminder_days")
        if DataManager.set_expiry_reminder_days(days):
            audited = _audit_result(
                audit_id, user_id, "config.expiry_reminder_set", "success",
                "system_setting", "expiry_reminder_days",
                before={"days": before}, after={"days": days},
            )
            await event.answer(t(language, "admin.reminder.saved", days=days))
            await admin_expiry_reminder_settings(event)
            if not audited:
                await event.respond(t(language, "admin.common.audit_warning"))
        else:
            _audit_result(
                audit_id, user_id, "config.expiry_reminder_set", "failed",
                "system_setting", "expiry_reminder_days", before={"days": before}, error="save_failed",
            )
            await event.answer(t(language, "admin.reminder.failed"), alert=True)

    @bot.on(events.CallbackQuery(pattern=b"admin_subscription_grant_help"))
    async def admin_subscription_grant_help(event):
        if not await require_admin(event, alert=True):
            return

        _clear_admin_input_state(event.sender_id)
        language = _language(event.sender_id)
        await event.answer()
        await safe_edit(
            event,
            t(language, "admin.subscription.help_grant"),
            buttons=[[back_button(b"admin_panel", language=language)]], parse_mode='md',
        )

    @bot.on(events.NewMessage(pattern=r'^/sub(?:@\w+)?(?:\s+.*)?$'))
    async def sub_command(event):
        if not await require_admin(event):
            return
        language = _language(event.sender_id)
        args = (event.text or '').split()[1:]
        target_hint = args[0] if args else None
        audit_id = AdminAuditLog.record_attempt(event.sender_id, "subscription.grant", "user", target_hint)
        if len(args) < 3:
            _audit_result(audit_id, event.sender_id, "subscription.grant", "failed", "user", target_hint, error="invalid_argument_count")
            await event.respond(t(language, "admin.subscription.sub_format"))
            return
        try:
            target_user_id = int(args[0])
            if target_user_id <= 0:
                raise ValueError(t(language, "admin.subscription.error.user_id_positive"))
            if DataManager.is_admin(target_user_id):
                raise ValueError(t(language, "admin.subscription.error.admin_unlimited"))
            plan_id = args[1].lower()
            if plan_id not in {'go', 'plus', 'pro'}:
                raise ValueError(t(language, "admin.subscription.error.plan"))
            days = int(args[2])
            if days <= 0:
                raise ValueError(t(language, "admin.subscription.error.days_positive"))
            try:
                datetime.now() + timedelta(days=days)
            except (OverflowError, ValueError):
                raise ValueError(t(language, "admin.subscription.error.days_range"))
            quota = None
            if plan_id == 'plus':
                if len(args) > 4:
                    raise ValueError(t(language, "admin.subscription.error.plus_args"))
                quota = int(args[3]) if len(args) == 4 else None
            elif len(args) != 3:
                raise ValueError(t(
                    language, "admin.subscription.error.quota_not_allowed",
                    plan=plan_id.upper(),
                ))
            quote = DataManager.quote_subscription(plan_id, quota)
        except (TypeError, ValueError) as error:
            _audit_result(audit_id, event.sender_id, "subscription.grant", "failed", "user", target_hint, error=str(error))
            await event.respond(t(language, "admin.subscription.invalid_params", error=error))
            return
        before = DataManager.get_subscription(target_user_id, include_inactive=True)
        pending = _queue_admin_action(
            event.sender_id, "subscription.grant", target_user_id,
            {"plan_id": plan_id, "days": days, "quota": quote["quota"]},
            before, audit_id=audit_id,
        )
        set_state(event.sender_id, admin_user_back="admin_panel")
        await event.respond(
            _grant_confirmation_text(target_user_id, quote, days, before, language),
            buttons=_confirmation_buttons(pending, language),
        )

    @bot.on(events.NewMessage(pattern=r'^/delsub(?:@\w+)?(?:\s+.*)?$'))
    async def delsub_command(event):
        if not await require_admin(event):
            return
        language = _language(event.sender_id)
        args = (event.text or '').split()[1:]
        target_hint = args[0] if args else None
        audit_id = AdminAuditLog.record_attempt(event.sender_id, "subscription.delete", "user", target_hint)
        if len(args) != 1:
            _audit_result(audit_id, event.sender_id, "subscription.delete", "failed", "user", target_hint, error="invalid_argument_count")
            await event.respond(t(language, "admin.subscription.delsub_format"))
            return
        try:
            target_user_id = int(args[0])
            if target_user_id <= 0:
                raise ValueError(t(language, "admin.subscription.error.user_id_positive"))
            if DataManager.is_admin(target_user_id):
                raise ValueError(t(language, "admin.subscription.error.delete_admin"))
        except ValueError as error:
            _audit_result(audit_id, event.sender_id, "subscription.delete", "failed", "user", target_hint, error=str(error))
            await event.respond(t(language, "admin.subscription.invalid_params", error=error))
            return

        before = DataManager.get_subscription(target_user_id, include_inactive=True)
        if not before:
            _audit_result(audit_id, event.sender_id, "subscription.delete", "failed", "user", target_user_id, error="no_subscription")
            await event.respond(
                t(language, "admin.subscription.nothing_to_delete", user_id=target_user_id),
                parse_mode='md'
            )
            return
        pending = _queue_admin_action(
            event.sender_id, "subscription.delete", target_user_id,
            {"account_keys": sorted(user_accounts.get(target_user_id, {}))}, before,
            audit_id=audit_id,
        )
        set_state(event.sender_id, admin_user_back="admin_panel")
        await event.respond(
            t(
                language, "admin.subscription.delete_command_confirm",
                user_id=target_user_id,
                plan=str(before.get("plan_id") or "-").upper(),
                expires_at=str(before.get("expires_at") or "-")[:19],
            ),
            buttons=_confirmation_buttons(pending, language),
        )

    @bot.on(events.CallbackQuery(pattern=rb"admin_list_vip(?:_\d+)?"))
    async def admin_list_vip(event):
        user_id = event.sender_id
        language = _language(user_id)
        data = event.data.decode(errors="ignore")
        page = int(data.rsplit("_", 1)[1]) if data.startswith("admin_list_vip_") else 0
        
        if not await require_admin(event, alert=True):
            return
        _clear_admin_input_state(user_id)
        
        vip_users = DataManager.get_all_subscription_users()
        
        if not vip_users:
            await event.answer()
            await edit_or_respond(
                event, t(language, "admin.subscription.list_empty"),
                buttons=[[back_button(b"admin_panel", language=language)]],
            )
            return

        page_items, page, max_page = paginate_items(vip_users, page, page_size=25)
        buttons = []
        for vip in page_items:
            vip_user_id = vip['user_id']
            name = await _get_user_display_name(bot, vip_user_id)
            hosting_count = AccountManager.get_quota_status(vip_user_id)['used']
            label_parts = []
            if name:
                label_parts.append(name)
            label_parts.extend([
                f"ID:{vip_user_id}",
                t(language, "admin.subscription.hosted_label", count=hosting_count),
            ])
            buttons.append([
                Button.inline(
                    " | ".join(label_parts),
                    f"admin_vip_user_{vip_user_id}".encode()
                )
            ])

        nav = pagination_buttons("admin_list_vip", page, max_page, language)
        if nav:
            buttons.append(nav)
        buttons.append([back_button(b"admin_panel", language=language)])
        response = t(language, "admin.subscription.list_text", count=len(vip_users))

        await event.answer()
        await edit_or_respond(event, response, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=rb"admin_vip_user_\d+"))
    async def admin_vip_user(event):
        if not await require_admin(event, alert=True):
            return
        target_user_id = int(event.data.decode().removeprefix("admin_vip_user_"))
        set_state(event.sender_id, admin_user_back="admin_list_vip")
        await _render_user_detail(event, target_user_id)

    @bot.on(events.CallbackQuery(pattern=b"admin_subscription_delete_help"))
    async def admin_subscription_delete_help(event):
        if not await require_admin(event, alert=True):
            return

        _clear_admin_input_state(event.sender_id)
        language = _language(event.sender_id)
        await event.answer()
        await safe_edit(
            event,
            t(language, "admin.subscription.help_delete"),
            buttons=[[back_button(b"admin_panel", language=language)]],
            parse_mode='md',
        )


