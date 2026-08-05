# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import json
import os
import logging
import shutil
import tempfile
import threading
import copy
import math
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Dict, List, Optional
import settings as config
from localization import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, normalize_language
from user_timezones import SUPPORTED_TIMEZONES, default_timezone

logger = logging.getLogger(__name__)

# 使用配置中的常量
ADMIN_IDS = config.ADMIN_IDS
DATA_FILE = config.DATA_FILE
PAYMENT_ORDERS_FILE = getattr(
    config, 'PAYMENT_ORDERS_FILE', os.path.join('storage', 'payment_orders.json')
)
USER_DATA_SCHEMA_VERSION = 1
PAYMENT_ORDERS_SCHEMA_VERSION = 1
_data_lock = threading.RLock()

# 全局存储
user_data: Dict[int, Dict] = {}
data_load_succeeded = False
payment_orders: Dict[str, Dict] = {}
payment_orders_load_succeeded = False

# 活跃订阅索引（user_id -> expiry_ts），用于加速到期查询。
# 注意：user_data 里既有 int 键（用户数据）也有 str 键（系统数据），这里仅缓存 int 用户。
subscription_expiry_index: Dict[int, float] = {}


class UnsupportedSchemaError(ValueError):
    pass


class DataManager:
    """数据管理类"""

    @staticmethod
    def is_data_ready() -> bool:
        """Return whether runtime user data was loaded successfully."""
        return bool(data_load_succeeded)

    SUBSCRIPTION_BADGES = {
        'go': '🥉',
        'plus': '🥈',
        'pro': '🥇',
        'admin': '👑',
    }

    @staticmethod
    def get_subscription_badge(plan_id: str) -> str:
        """返回套餐等级标识；皇冠仅用于管理员。"""
        return DataManager.SUBSCRIPTION_BADGES.get(str(plan_id).lower(), '✦')

    @staticmethod
    def _default_data() -> Dict:
        return {
            'schema_version': USER_DATA_SCHEMA_VERSION,
            'subscription_catalog': DataManager.default_subscription_catalog(),
            'subscription_periods': DataManager.default_subscription_periods(),
            'system_settings': {
                'expiry_reminder_days': 3,
                'login_unlock_reminder_schedule': {
                    'count': 1,
                    'offsets_seconds': [120],
                },
                'created_time': datetime.now().isoformat()
            }
        }

    @staticmethod
    def default_subscription_catalog() -> Dict:
        return {
            'go': {'name': 'Go', 'price': '0.6', 'quota': 2, 'coin': 'USDT'},
            'plus': {
                'name': 'Plus', 'price': '1', 'quota': 10, 'coin': 'USDT',
                'addon_unit_price': '0.1', 'min_addon': 5,
            },
            'pro': {'name': 'Pro', 'price': '3', 'quota': None, 'coin': 'USDT'},
        }

    @staticmethod
    def default_subscription_periods() -> Dict:
        return {
            30: {'discount_percent': '0'},
            90: {'discount_percent': '8'},
            180: {'discount_percent': '18'},
            365: {'discount_percent': '25'},
        }

    @staticmethod
    def _backup_data_file(reason: str = "invalid") -> str:
        if not os.path.exists(DATA_FILE):
            return ""

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = f"{DATA_FILE}.{reason}.{timestamp}.backup"
        suffix = 1
        while os.path.exists(backup_path):
            backup_path = f"{DATA_FILE}.{reason}.{timestamp}.{suffix}.backup"
            suffix += 1

        shutil.copy2(DATA_FILE, backup_path)
        logger.info(f"用户数据备份已创建: {backup_path}")
        return backup_path
    
    @staticmethod
    def load_user_data() -> bool:
        """加载当前 schema 的用户数据；旧格式必须先由离线迁移工具处理。"""
        global user_data, data_load_succeeded
        data_load_succeeded = False
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, 'r', encoding='utf-8') as f:
                    loaded_data = json.load(f)
                if not isinstance(loaded_data, dict):
                    raise ValueError('用户数据文件顶层必须是对象')
                if loaded_data.get('schema_version') != USER_DATA_SCHEMA_VERSION:
                    raise UnsupportedSchemaError(
                        '用户数据 schema 不受支持，请先运行 migrate_runtime_data.py --apply'
                    )

                user_data = {}
                for key, value in loaded_data.items():
                    if key in {
                        'schema_version', 'subscription_catalog',
                        'subscription_periods', 'system_settings',
                    }:
                        user_data[key] = value
                    else:
                        try:
                            user_id = int(key)
                            if not isinstance(value, dict):
                                raise ValueError(f'用户 {key} 的数据必须是对象')
                            legacy_fields = {
                                'is_vip', 'vip_expiry', 'vip_added', 'vip_days',
                            }.intersection(value)
                            if legacy_fields:
                                raise ValueError(
                                    f'用户 {key} 仍包含旧 VIP 字段: '
                                    + ', '.join(sorted(legacy_fields))
                                )
                            user_data[user_id] = value
                        except (TypeError, ValueError) as error:
                            if str(key).isdigit():
                                raise
                            raise ValueError(f'未知用户数据键: {key}') from error
                
                user_data.setdefault('subscription_catalog', DataManager.default_subscription_catalog())
                user_data.setdefault('subscription_periods', DataManager.default_subscription_periods())
                user_count = len([k for k in user_data.keys() if isinstance(k, int)])
                logger.info(f"✅ 已加载 {user_count} 个用户数据")
                DataManager.rebuild_subscription_index()
                data_load_succeeded = True
                if not DataManager.load_payment_orders():
                    data_load_succeeded = False
                    return False
                return True
            else:
                user_data = DataManager._default_data()
                data_load_succeeded = True
                if not DataManager.load_payment_orders():
                    data_load_succeeded = False
                    return False
                DataManager.save_user_data()
                DataManager.rebuild_subscription_index()
                logger.debug("📝 用户数据文件不存在，已创建空数据")
                return True
        except UnsupportedSchemaError as e:
            logger.error(f"❌ 加载用户数据失败: {str(e)}")
            user_data = DataManager._default_data()
            DataManager.rebuild_subscription_index()
            data_load_succeeded = False
            return False
        except Exception as e:
            logger.error(f"❌ 加载用户数据失败: {str(e)}")
            try:
                DataManager._backup_data_file("load-failed")
            except Exception as backup_error:
                logger.error(f"❌ 备份数据文件失败: {str(backup_error)}")
            user_data = DataManager._default_data()
            DataManager.rebuild_subscription_index()
            data_load_succeeded = False
            return False
    
    @staticmethod
    def save_user_data():
        """保存用户数据 - 修复版本"""
        tmp_path = None
        try:
            if not data_load_succeeded:
                logger.error("拒绝保存用户数据：load_user_data() 尚未成功完成")
                return False

            # 准备保存的数据
            save_data = {'schema_version': USER_DATA_SCHEMA_VERSION}
            
            for key, value in user_data.items():
                if key in {'payment_orders', 'vip_prices'}:
                    continue
                if isinstance(key, int):
                    legacy_fields = {
                        'is_vip', 'vip_expiry', 'vip_added', 'vip_days',
                    }.intersection(value)
                    if legacy_fields:
                        raise ValueError(
                            f'用户 {key} 仍包含旧 VIP 字段: '
                            + ', '.join(sorted(legacy_fields))
                        )
                    # 用户数据，使用字符串键
                    save_data[str(key)] = value
                else:
                    # 系统数据，直接保存
                    save_data[key] = value
            
            data_dir = os.path.dirname(os.path.abspath(DATA_FILE)) or "."
            os.makedirs(data_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=os.path.basename(DATA_FILE) + ".",
                suffix=".tmp",
                dir=data_dir,
                text=True
            )

            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(save_data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, DATA_FILE)
            tmp_path = None
            logger.info("✅ 用户数据已保存")
            return True
        except Exception as e:
            logger.error(f"❌ 保存用户数据失败: {str(e)}")
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return False
    

    @staticmethod
    def rebuild_subscription_index():
        """重建活跃订阅到期索引。"""
        global subscription_expiry_index
        subscription_expiry_index = {}
        now_ts = datetime.now().timestamp()
        for key, info in user_data.items():
            if not isinstance(key, int):
                continue
            if not info:
                continue
            subscription = info.get('subscription') or {}
            expiry_str = subscription.get('expires_at')
            if not expiry_str:
                continue
            try:
                expiry_ts = datetime.fromisoformat(expiry_str).timestamp()
            except Exception:
                continue
            # 仅保留未过期（或刚过期不久）的也行；这里保留未过期的即可
            if expiry_ts > now_ts:
                subscription_expiry_index[key] = expiry_ts

    @staticmethod
    def _set_subscription_index(
        user_id: int, expiry_date: datetime, active: bool = True
    ) -> None:
        """更新单个用户的活跃订阅索引。"""
        global subscription_expiry_index
        if not isinstance(user_id, int):
            return
        if not active:
            subscription_expiry_index.pop(user_id, None)
            return
        try:
            subscription_expiry_index[user_id] = expiry_date.timestamp()
        except Exception:
            # 不影响主流程
            return

    @staticmethod
    def iter_subscription_users():
        """遍历活跃订阅用户。"""
        global subscription_expiry_index
        if not subscription_expiry_index:
            # 可能还没构建，尝试构建一次
            DataManager.rebuild_subscription_index()
        # 使用 list() 避免迭代过程中被更新引发 RuntimeError
        for user_id, expiry_ts in list(subscription_expiry_index.items()):
            yield user_id, datetime.fromtimestamp(expiry_ts)
    @staticmethod
    def is_admin(user_id: int) -> bool:
        """检查是否为管理员"""
        return user_id in ADMIN_IDS

    @staticmethod
    def get_user_language(user_id: int) -> str:
        """Return a persisted supported language, falling back without writing."""
        info = user_data.get(int(user_id), {})
        language = info.get('language') if isinstance(info, dict) else None
        return language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

    @staticmethod
    def has_user_language(user_id: int) -> bool:
        info = user_data.get(int(user_id), {})
        return isinstance(info, dict) and info.get('language') in SUPPORTED_LANGUAGES

    @staticmethod
    def initialize_user_language(user_id: int, telegram_language_code=None) -> bool:
        """Persist an inferred language once; never overwrite a user's choice."""
        if not data_load_succeeded:
            # Handler unit tests may run before application bootstrap. Production
            # always loads data before registering or dispatching handlers.
            return True
        user_id = int(user_id)
        if DataManager.has_user_language(user_id):
            return True
        existed = user_id in user_data
        previous = copy.deepcopy(user_data.get(user_id)) if existed else None
        info = user_data.setdefault(user_id, {})
        info['language'] = normalize_language(telegram_language_code)
        if DataManager.save_user_data():
            return True
        if existed:
            user_data[user_id] = previous
        else:
            user_data.pop(user_id, None)
        return False

    @staticmethod
    def set_user_language(user_id: int, language: str) -> bool:
        """Atomically persist an explicit language selection."""
        if language not in SUPPORTED_LANGUAGES:
            return False
        user_id = int(user_id)
        existed = user_id in user_data
        previous = copy.deepcopy(user_data.get(user_id)) if existed else None
        info = user_data.setdefault(user_id, {})
        info['language'] = language
        if DataManager.save_user_data():
            return True
        if existed:
            user_data[user_id] = previous
        else:
            user_data.pop(user_id, None)
        return False

    @staticmethod
    def get_user_timezone(user_id: int) -> str:
        """Return an explicit timezone, or infer a default from UI language."""
        info = user_data.get(int(user_id), {})
        selected = info.get('timezone') if isinstance(info, dict) else None
        if selected in SUPPORTED_TIMEZONES:
            return selected
        return default_timezone(DataManager.get_user_language(user_id))

    @staticmethod
    def set_user_timezone(user_id: int, timezone_name: str) -> bool:
        """Atomically persist an explicit supported timezone selection."""
        if timezone_name not in SUPPORTED_TIMEZONES:
            return False
        user_id = int(user_id)
        existed = user_id in user_data
        previous = copy.deepcopy(user_data.get(user_id)) if existed else None
        info = user_data.setdefault(user_id, {})
        info['timezone'] = timezone_name
        if DataManager.save_user_data():
            return True
        if existed:
            user_data[user_id] = previous
        else:
            user_data.pop(user_id, None)
        return False
    
    @staticmethod
    def has_active_subscription(user_id: int) -> bool:
        """检查用户是否有活跃订阅。"""
        global subscription_expiry_index
        ts = subscription_expiry_index.get(user_id)
        if ts is not None:
            return datetime.now().timestamp() < ts

        if user_id not in user_data:
            return False

        user_info = user_data[user_id]
        subscription_expiry = (user_info.get('subscription') or {}).get('expires_at')
        if subscription_expiry:
            try:
                expiry_ts = datetime.fromisoformat(subscription_expiry).timestamp()
                if expiry_ts > datetime.now().timestamp():
                    subscription_expiry_index[user_id] = expiry_ts
                    return True
                return False
            except (TypeError, ValueError):
                return False
        return False

    @staticmethod
    def get_subscription_catalog() -> Dict:
        catalog = copy.deepcopy(
            user_data.get(
                'subscription_catalog', DataManager.default_subscription_catalog()
            )
        )
        defaults = DataManager.default_subscription_catalog()
        for plan_id, default in defaults.items():
            current = catalog.setdefault(plan_id, {})
            for key, value in default.items():
                current.setdefault(key, value)
        return catalog

    @staticmethod
    def set_subscription_catalog(catalog: Dict) -> bool:
        previous = copy.deepcopy(user_data.get('subscription_catalog'))
        try:
            normalized = copy.deepcopy(DataManager.default_subscription_catalog())
            for plan_id in ('go', 'plus', 'pro'):
                source = catalog.get(plan_id, {})
                price = Decimal(str(source.get('price', normalized[plan_id]['price'])))
                if price <= 0:
                    return False
                normalized[plan_id]['price'] = DataManager._decimal_text(price)
                if plan_id != 'pro':
                    quota = int(source.get('quota', normalized[plan_id]['quota']))
                    if quota <= 0:
                        return False
                    normalized[plan_id]['quota'] = quota
            plus = catalog.get('plus', {})
            addon_price = Decimal(str(plus.get('addon_unit_price', normalized['plus']['addon_unit_price'])))
            min_addon = int(plus.get('min_addon', normalized['plus']['min_addon']))
            if addon_price <= 0 or min_addon <= 0:
                return False
            normalized['plus']['addon_unit_price'] = DataManager._decimal_text(addon_price)
            normalized['plus']['min_addon'] = min_addon
            user_data['subscription_catalog'] = normalized
            if DataManager.save_user_data():
                return True
            if previous is None:
                user_data.pop('subscription_catalog', None)
            else:
                user_data['subscription_catalog'] = previous
            return False
        except (InvalidOperation, TypeError, ValueError):
            return False

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        value = value.quantize(Decimal('0.00000001')).normalize()
        text = format(value, 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text

    @staticmethod
    def get_subscription_periods() -> Dict:
        stored = user_data.get(
            'subscription_periods', DataManager.default_subscription_periods()
        )
        defaults = DataManager.default_subscription_periods()
        normalized = {}
        for days, default in defaults.items():
            source = stored.get(days, stored.get(str(days), default))
            try:
                discount = Decimal(str(source.get('discount_percent', default['discount_percent'])))
            except (AttributeError, InvalidOperation, TypeError, ValueError):
                discount = Decimal(default['discount_percent'])
            normalized[days] = {'discount_percent': DataManager._decimal_text(discount)}
        return normalized

    @staticmethod
    def set_subscription_periods(periods: Dict) -> bool:
        previous = copy.deepcopy(user_data.get('subscription_periods'))
        try:
            normalized = {}
            for days in (30, 90, 180, 365):
                source = periods.get(days, periods.get(str(days)))
                if source is None:
                    return False
                value = source.get('discount_percent') if isinstance(source, dict) else source
                discount = Decimal(str(value))
                if discount < 0 or discount >= 100 or (days == 30 and discount != 0):
                    return False
                normalized[days] = {
                    'discount_percent': DataManager._decimal_text(discount)
                }
            user_data['subscription_periods'] = normalized
            if DataManager.save_user_data():
                return True
            if previous is None:
                user_data.pop('subscription_periods', None)
            else:
                user_data['subscription_periods'] = previous
            return False
        except (AttributeError, InvalidOperation, TypeError, ValueError):
            return False

    @staticmethod
    def quote_subscription(
        plan_id: str, quota: Optional[int] = None, period_days: int = 30
    ) -> Dict:
        plan_id = str(plan_id).lower()
        catalog = DataManager.get_subscription_catalog()
        if plan_id not in catalog:
            raise ValueError('未知订阅套餐')
        plan = catalog[plan_id]
        base_quota = plan.get('quota')
        addon = 0
        actual_quota = base_quota
        price = Decimal(str(plan['price']))
        if plan_id == 'plus':
            actual_quota = int(base_quota if quota is None else quota)
            addon = actual_quota - int(base_quota)
            if addon < 0:
                raise ValueError('Plus 配额不能低于基础配额')
            if addon != 0 and addon < int(plan['min_addon']):
                raise ValueError(f"Plus 扩容至少增加 {plan['min_addon']} 个配额")
            price += Decimal(addon) * Decimal(str(plan['addon_unit_price']))
        elif quota is not None and base_quota is not None and int(quota) != int(base_quota):
            raise ValueError('该套餐不支持自定义配额')
        try:
            period_days = int(period_days)
        except (TypeError, ValueError):
            raise ValueError('订阅周期无效')
        periods = DataManager.get_subscription_periods()
        if period_days not in periods:
            raise ValueError('不支持该订阅周期')
        monthly_price = price
        pricing_days = 360 if period_days == 365 else period_days
        list_price = monthly_price * Decimal(pricing_days) / Decimal(30)
        # GO always charges its full proportional price. Long-period discounts
        # are reserved for PLUS and PRO.
        discount_percent = (
            Decimal('0')
            if plan_id == 'go'
            else Decimal(periods[period_days]['discount_percent'])
        )
        theoretical_price = list_price * (Decimal(100) - discount_percent) / Decimal(100)
        if period_days == 30 or plan_id == 'go':
            final_price = theoretical_price
        else:
            final_price = (theoretical_price * Decimal(2)).to_integral_value(
                rounding=ROUND_DOWN
            ) / Decimal(2)
            final_price = max(Decimal('0.5'), final_price)
        discount_amount = max(Decimal('0'), list_price - final_price)
        actual_discount_percent = (
            discount_amount * Decimal(100) / list_price if list_price > 0 else Decimal('0')
        )
        effective_monthly_price = final_price * Decimal(30) / Decimal(period_days)
        return {
            'plan_id': plan_id,
            'plan_name': plan['name'],
            'quota': actual_quota,
            'addon': addon,
            'price': DataManager._decimal_text(final_price),
            'monthly_catalog_price': DataManager._decimal_text(monthly_price),
            'list_price': DataManager._decimal_text(list_price),
            'configured_discount_percent': DataManager._decimal_text(discount_percent),
            'theoretical_price': DataManager._decimal_text(theoretical_price),
            'discount_amount': DataManager._decimal_text(discount_amount),
            'actual_discount_percent': DataManager._decimal_text(actual_discount_percent),
            'effective_monthly_price': DataManager._decimal_text(effective_monthly_price),
            'coin': 'USDT',
            'period_days': period_days,
            'pricing_days': pricing_days,
        }

    @staticmethod
    def _raw_subscription_state(subscription: Dict) -> Dict:
        return {
            'plan_id': subscription.get('plan_id'),
            'quota': subscription.get('quota'),
            'expires_at': subscription.get('expires_at'),
            'billing_segments': copy.deepcopy(subscription.get('billing_segments') or []),
        }

    @staticmethod
    def _remaining_billing_segments(
        subscription: Dict, now: Optional[datetime] = None
    ) -> List[Dict]:
        now = now or datetime.now()
        raw_segments = subscription.get('billing_segments') or []
        segments = []
        for raw in raw_segments:
            try:
                starts = datetime.fromisoformat(raw['starts_at'])
                expires = datetime.fromisoformat(raw['expires_at'])
                monthly_price = Decimal(str(raw['monthly_price']))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
            if expires <= now or expires <= starts or monthly_price <= 0:
                continue
            segment = copy.deepcopy(raw)
            segment['starts_at'] = max(starts, now).isoformat()
            segment['expires_at'] = expires.isoformat()
            segment['monthly_price'] = DataManager._decimal_text(monthly_price)
            segments.append(segment)
        if segments:
            return sorted(segments, key=lambda item: item['starts_at'])

        try:
            expires = datetime.fromisoformat(subscription['expires_at'])
            quote = DataManager.quote_subscription(
                subscription['plan_id'], subscription.get('quota')
            )
        except (KeyError, TypeError, ValueError):
            return []
        if expires <= now:
            return []
        return [{
            'starts_at': now.isoformat(),
            'expires_at': expires.isoformat(),
            'plan_id': subscription.get('plan_id'),
            'quota': subscription.get('quota'),
            'monthly_price': quote['price'],
            'price_source': 'catalog_fallback',
        }]

    @staticmethod
    def quote_subscription_upgrade(
        user_id: int, plan_id: str, quota: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Dict:
        now = now or datetime.now()
        current = DataManager.get_subscription(user_id)
        if not current or not current.get('active'):
            raise ValueError('当前没有可抵扣的有效订阅')
        if current.get('scheduled'):
            raise ValueError('已有待生效方案，无法创建差价升级订单')
        target = DataManager.quote_subscription(plan_id, quota)
        if DataManager.classify_subscription_change(
            user_id, target['plan_id'], target['quota']
        ) != 'upgrade':
            raise ValueError('目标方案不是当前订阅的升级方案')

        segments = DataManager._remaining_billing_segments(current, now)
        if not segments:
            raise ValueError('订阅剩余期限无效')
        target_monthly = Decimal(str(target['price']))
        source_value = Decimal('0')
        target_value = Decimal('0')
        billable_days = 0
        breakdown = []
        target_segments = []
        for segment in segments:
            starts = datetime.fromisoformat(segment['starts_at'])
            expires = datetime.fromisoformat(segment['expires_at'])
            days = max(1, math.ceil((expires - starts).total_seconds() / 86400))
            source_monthly = Decimal(str(segment['monthly_price']))
            if target_monthly <= source_monthly:
                raise ValueError('目标方案价格必须高于当前订阅成交价')
            source_amount = source_monthly * Decimal(days) / Decimal(30)
            target_amount = target_monthly * Decimal(days) / Decimal(30)
            source_value += source_amount
            target_value += target_amount
            billable_days += days
            breakdown.append({
                'starts_at': segment['starts_at'],
                'expires_at': segment['expires_at'],
                'billable_days': days,
                'source_monthly_price': DataManager._decimal_text(source_monthly),
                'source_value': DataManager._decimal_text(source_amount),
                'target_value': DataManager._decimal_text(target_amount),
                'price_source': segment.get('price_source', 'order'),
            })
            target_segments.append({
                'starts_at': segment['starts_at'],
                'expires_at': segment['expires_at'],
                'plan_id': target['plan_id'],
                'quota': target['quota'],
                'monthly_price': target['price'],
                'price_source': 'prorated_upgrade',
            })
        amount = target_value - source_value
        amount_text = DataManager._decimal_text(amount)
        if Decimal(amount_text) <= 0:
            raise ValueError('升级差价必须大于0')
        return {
            'billing_mode': 'prorated_upgrade',
            'source_plan_id': current.get('plan_id'),
            'source_quota': current.get('quota'),
            'source_state': DataManager._raw_subscription_state(current),
            'source_value': DataManager._decimal_text(source_value),
            'target_value': DataManager._decimal_text(target_value),
            'target_plan_id': target['plan_id'],
            'target_quota': target['quota'],
            'target_monthly_price': target['price'],
            'target_expires_at': current['expires_at'],
            'target_segments': target_segments,
            'billable_days': billable_days,
            'breakdown': breakdown,
            'amount': amount_text,
            'coin': 'USDT',
            'quoted_at': now.isoformat(),
            'uses_catalog_fallback': any(
                item.get('price_source') == 'catalog_fallback' for item in breakdown
            ),
        }

    @staticmethod
    def apply_prorated_upgrade(user_id: int, upgrade_snapshot: Dict) -> bool:
        current = DataManager.get_subscription(user_id)
        if not current or not current.get('active'):
            return False
        if DataManager._raw_subscription_state(current) != upgrade_snapshot.get('source_state'):
            return False
        try:
            expiry = datetime.fromisoformat(upgrade_snapshot['target_expires_at'])
            plan_id = str(upgrade_snapshot['target_plan_id'])
            quota = upgrade_snapshot.get('target_quota')
            target_segments = copy.deepcopy(upgrade_snapshot['target_segments'])
        except (KeyError, TypeError, ValueError):
            return False
        if expiry <= datetime.now() or plan_id not in {'go', 'plus', 'pro'}:
            return False
        if plan_id == 'pro':
            quota = None
        else:
            try:
                quota = int(quota)
            except (TypeError, ValueError):
                return False
            if quota <= 0:
                return False
        info = user_data.get(int(user_id), {})
        subscription = copy.deepcopy(info.get('subscription') or {})
        subscription.update({
            'plan_id': plan_id,
            'quota': quota,
            'starts_at': datetime.now().isoformat(),
            'expires_at': expiry.isoformat(),
            'billing_segments': target_segments,
            'selection_required': False,
        })
        subscription.pop('scheduled', None)
        info['subscription'] = subscription
        info['last_updated'] = datetime.now().isoformat()
        DataManager._set_subscription_index(user_id, expiry, True)
        return True

    @staticmethod
    def _activate_scheduled_subscription(user_id: int, now: Optional[datetime] = None) -> bool:
        info = user_data.get(user_id, {})
        subscription = info.get('subscription') or {}
        scheduled = subscription.get('scheduled')
        if not scheduled:
            return False
        now = now or datetime.now()
        try:
            effective = datetime.fromisoformat(scheduled['starts_at'])
        except (KeyError, TypeError, ValueError):
            return False
        if effective > now:
            return False
        is_downgrade = DataManager._is_quota_downgrade(
            subscription.get('quota'), scheduled.get('quota')
        )
        selected = [] if is_downgrade else subscription.get('selected_accounts', [])
        info['subscription'] = {
            'plan_id': scheduled['plan_id'], 'quota': scheduled.get('quota'),
            'starts_at': scheduled['starts_at'], 'expires_at': scheduled['expires_at'],
            'selected_accounts': selected, 'selection_required': is_downgrade,
            'billing_segments': DataManager._remaining_billing_segments(subscription, now),
        }
        info['last_updated'] = now.isoformat()
        return True

    @staticmethod
    def get_subscription(user_id: int, include_inactive: bool = False) -> Optional[Dict]:
        if DataManager.is_admin(user_id):
            return {'plan_id': 'admin', 'plan_name': 'Admin', 'quota': None, 'active': True}
        info = user_data.get(user_id, {})
        subscription = copy.deepcopy(info.get('subscription') or {})
        if not subscription:
            return None
        try:
            active = datetime.fromisoformat(subscription['expires_at']) > datetime.now()
        except (KeyError, TypeError, ValueError):
            active = False
        subscription['active'] = active
        subscription['plan_name'] = DataManager.get_subscription_catalog().get(
            subscription.get('plan_id'), {}
        ).get('name', subscription.get('plan_id', ''))
        return subscription if active or include_inactive else None

    @staticmethod
    def get_hosting_quota(user_id: int) -> Optional[int]:
        subscription = DataManager.get_subscription(user_id)
        return subscription.get('quota') if subscription else 0

    @staticmethod
    def classify_subscription_change(user_id: int, plan_id: str, quota: Optional[int]) -> str:
        current = DataManager.get_subscription(user_id)
        if not current:
            return 'new'
        if current.get('scheduled'):
            scheduled = current['scheduled']
            if scheduled.get('plan_id') == plan_id and scheduled.get('quota') == quota:
                return 'scheduled_renewal'
            return 'conflict'
        current_score = float('inf') if current.get('quota') is None else int(current['quota'])
        new_score = float('inf') if quota is None else int(quota)
        if current.get('plan_id') == plan_id and current.get('quota') == quota:
            return 'renewal'
        return 'upgrade' if new_score > current_score else 'downgrade'

    @staticmethod
    def _is_quota_downgrade(old_quota: Optional[int], new_quota: Optional[int]) -> bool:
        """Return whether a quota change reduces the number of hosted accounts."""
        old_score = float('inf') if old_quota is None else int(old_quota)
        new_score = float('inf') if new_quota is None else int(new_quota)
        return new_score < old_score

    @staticmethod
    def apply_subscription(
        user_id: int, plan_id: str, quota: Optional[int], days: int = 30,
        validate_catalog: bool = True, billing_price: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> bool:
        try:
            user_id = int(user_id)
            days = int(days)
        except (TypeError, ValueError):
            return False
        if user_id <= 0 or days <= 0 or DataManager.is_admin(user_id):
            return False
        plan_id = str(plan_id).lower()
        if validate_catalog:
            quote = DataManager.quote_subscription(plan_id, quota)
            quota = quote['quota']
        elif plan_id not in {'go', 'plus', 'pro'}:
            return False
        elif plan_id == 'pro':
            quota = None
        else:
            try:
                quota = int(quota)
            except (TypeError, ValueError):
                return False
            if quota <= 0:
                return False
        if billing_price is None:
            try:
                billing_price = DataManager.quote_subscription(plan_id, quota)['price']
            except ValueError:
                return False
        try:
            billing_price = DataManager._decimal_text(Decimal(str(billing_price)))
            if Decimal(billing_price) <= 0:
                return False
        except (InvalidOperation, TypeError, ValueError):
            return False
        now = datetime.now()
        info = user_data.setdefault(user_id, {})
        current = DataManager.get_subscription(user_id, include_inactive=True)
        remaining_segments = (
            DataManager._remaining_billing_segments(current, now)
            if current and current.get('active') else []
        )
        change = DataManager.classify_subscription_change(user_id, plan_id, quota)
        if change == 'conflict':
            return False
        if current and current.get('active') and change in {'downgrade', 'scheduled_renewal'}:
            scheduled = current.get('scheduled')
            starts = datetime.fromisoformat(current['expires_at'])
            if scheduled:
                end = datetime.fromisoformat(scheduled['expires_at']) + timedelta(days=days)
                segment_start = datetime.fromisoformat(scheduled['expires_at'])
            else:
                end = starts + timedelta(days=days)
                segment_start = starts
            subscription = copy.deepcopy(info['subscription'])
            subscription['scheduled'] = {
                'plan_id': plan_id, 'quota': quota, 'starts_at': starts.isoformat(),
                'expires_at': end.isoformat(),
            }
            segments = remaining_segments
            segments.append({
                'starts_at': segment_start.isoformat(),
                'expires_at': end.isoformat(),
                'plan_id': plan_id,
                'quota': quota,
                'monthly_price': billing_price,
                'price_source': 'order' if order_id else 'admin_grant',
                **({'order_id': order_id} if order_id else {}),
            })
            subscription['billing_segments'] = segments
            info['subscription'] = subscription
        else:
            base = now
            if current and current.get('active'):
                base = datetime.fromisoformat(current['expires_at'])
            expiry = base + timedelta(days=days)
            is_reactivated_downgrade = bool(
                current
                and not current.get('active')
                and DataManager._is_quota_downgrade(current.get('quota'), quota)
            )
            if current and current.get('active') and change == 'upgrade':
                remaining_segments = [{
                    **segment,
                    'plan_id': plan_id,
                    'quota': quota,
                    'monthly_price': billing_price,
                    'price_source': 'admin_grant',
                } for segment in remaining_segments]
            segment = {
                'starts_at': base.isoformat(),
                'expires_at': expiry.isoformat(),
                'plan_id': plan_id,
                'quota': quota,
                'monthly_price': billing_price,
                'price_source': 'order' if order_id else 'admin_grant',
                **({'order_id': order_id} if order_id else {}),
            }
            billing_segments = remaining_segments + [segment]
            info['subscription'] = {
                'plan_id': plan_id, 'quota': quota, 'starts_at': now.isoformat(),
                'expires_at': expiry.isoformat(),
                'selected_accounts': (
                    [] if is_reactivated_downgrade
                    else (current or {}).get('selected_accounts', [])
                ),
                'selection_required': is_reactivated_downgrade,
                'billing_segments': billing_segments,
            }
            DataManager._set_subscription_index(user_id, expiry, True)
        info['last_updated'] = now.isoformat()
        return True

    @staticmethod
    def grant_subscription(
        user_id: int, plan_id: str, days: int, quota: Optional[int] = None
    ) -> bool:
        """Apply and persist an administrator grant, rolling memory back on failure."""
        try:
            user_id = int(user_id)
            days = int(days)
        except (TypeError, ValueError):
            return False
        if user_id <= 0 or days <= 0 or DataManager.is_admin(user_id):
            return False

        existed = user_id in user_data
        previous = copy.deepcopy(user_data.get(user_id)) if existed else None
        try:
            if not DataManager.apply_subscription(user_id, plan_id, quota, days=days):
                raise ValueError("invalid subscription grant")
            if not DataManager.save_user_data():
                raise OSError("failed to persist subscription grant")
            return True
        except (ArithmeticError, OSError, OverflowError, TypeError, ValueError):
            if not existed:
                user_data.pop(user_id, None)
            else:
                user_data[user_id] = previous
            DataManager.rebuild_subscription_index()
            return False

    @staticmethod
    def get_subscription_user_ids() -> List[int]:
        return [
            user_id for user_id, info in user_data.items()
            if isinstance(user_id, int) and isinstance(info, dict) and info.get('subscription')
        ]

    @staticmethod
    def get_all_user_ids() -> List[int]:
        """Return all locally persisted user IDs without exposing mutable user data."""
        return sorted(user_id for user_id in user_data if isinstance(user_id, int))

    @staticmethod
    def activate_due_subscriptions() -> List[int]:
        previous = {}
        activated = []
        for user_id in DataManager.get_subscription_user_ids():
            previous[user_id] = copy.deepcopy(user_data[user_id])
            if DataManager._activate_scheduled_subscription(user_id):
                activated.append(user_id)
            else:
                previous.pop(user_id, None)
        if not activated:
            return []
        DataManager.rebuild_subscription_index()
        if DataManager.save_user_data():
            return activated
        for user_id, snapshot in previous.items():
            user_data[user_id] = snapshot
        DataManager.rebuild_subscription_index()
        logger.error("到期订阅转换保存失败，已回滚 %d 个用户", len(activated))
        return []

    @staticmethod
    def set_selected_accounts(
        user_id: int, phones: List[str], finalize: bool = True
    ) -> bool:
        info = user_data.get(int(user_id), {})
        subscription = info.get('subscription')
        if not subscription:
            return False
        normalized = []
        for phone in phones:
            digits = ''.join(char for char in str(phone) if char.isdigit())
            if digits and digits not in normalized:
                normalized.append(digits)
        quota = subscription.get('quota')
        if quota is not None and len(normalized) > int(quota):
            return False
        subscription['selected_accounts'] = normalized
        subscription['selection_required'] = not finalize
        return DataManager.save_user_data()

    @staticmethod
    def get_raw_subscription_snapshot(user_id: int) -> Optional[Dict]:
        """Return the persisted subscription exactly as stored for transaction CAS."""
        info = user_data.get(int(user_id))
        if not isinstance(info, dict) or not isinstance(info.get('subscription'), dict):
            return None
        return copy.deepcopy(info['subscription'])

    @staticmethod
    def subscription_snapshots_match(snapshots: Dict[int, Optional[Dict]]) -> bool:
        return all(
            DataManager.get_raw_subscription_snapshot(int(user_id)) == snapshot
            for user_id, snapshot in snapshots.items()
        )

    @staticmethod
    def restore_subscription_snapshots(
        snapshots: Dict[int, Optional[Dict]]
    ) -> bool:
        """Restore transfer-owned subscription changes with one durable save."""
        with _data_lock:
            previous = {
                int(user_id): copy.deepcopy(user_data.get(int(user_id)))
                for user_id in snapshots
            }
            try:
                for raw_user_id, snapshot in snapshots.items():
                    user_id = int(raw_user_id)
                    info = user_data.setdefault(user_id, {})
                    if snapshot is None:
                        info.pop('subscription', None)
                    else:
                        info['subscription'] = copy.deepcopy(snapshot)
                DataManager.rebuild_subscription_index()
                if DataManager.save_user_data():
                    return True
            except Exception:
                logger.exception("恢复账户转移订阅快照失败")
            for user_id, snapshot in previous.items():
                if snapshot is None:
                    user_data.pop(user_id, None)
                else:
                    user_data[user_id] = snapshot
            DataManager.rebuild_subscription_index()
            return False

    @staticmethod
    def transfer_selected_account(
        from_user_id: int,
        to_user_id: int,
        phone: str,
        expected_snapshots: Dict[int, Optional[Dict]],
        target_hosted_phones: List[str],
    ) -> bool:
        """Move a finite-plan seat between owners using compare-and-save semantics."""
        digits = ''.join(char for char in str(phone) if char.isdigit())
        if not digits:
            return False
        with _data_lock:
            if not DataManager.subscription_snapshots_match(expected_snapshots):
                return False
            previous = {
                int(user_id): copy.deepcopy(user_data.get(int(user_id)))
                for user_id in expected_snapshots
            }
            try:
                source = (user_data.get(int(from_user_id), {}) or {}).get('subscription')
                if isinstance(source, dict) and source.get('quota') is not None:
                    source['selected_accounts'] = [
                        item for item in source.get('selected_accounts') or []
                        if ''.join(char for char in str(item) if char.isdigit()) != digits
                    ]

                target = (user_data.get(int(to_user_id), {}) or {}).get('subscription')
                if isinstance(target, dict) and target.get('quota') is not None:
                    quota = int(target['quota'])
                    hosted = []
                    for item in target_hosted_phones:
                        normalized = ''.join(char for char in str(item) if char.isdigit())
                        if normalized and normalized not in hosted:
                            hosted.append(normalized)
                    if digits not in hosted:
                        hosted.append(digits)
                    selected = []
                    for item in target.get('selected_accounts') or []:
                        normalized = ''.join(char for char in str(item) if char.isdigit())
                        if normalized in hosted and normalized not in selected:
                            selected.append(normalized)
                    if target.get('selection_required'):
                        selected = sorted(hosted)
                    elif digits not in selected:
                        selected.append(digits)
                    if len(selected) > quota:
                        raise ValueError('target subscription quota changed during transfer')
                    target['selected_accounts'] = selected
                    target['selection_required'] = False

                if DataManager.save_user_data():
                    return True
            except Exception:
                logger.exception("保存账户转移订阅席位失败")
            for user_id, snapshot in previous.items():
                if snapshot is None:
                    user_data.pop(user_id, None)
                else:
                    user_data[user_id] = snapshot
            DataManager.rebuild_subscription_index()
            return False

    @staticmethod
    def reconcile_selected_accounts(hosted_by_user: Dict[int, List[str]]) -> bool:
        """Prune historical subscription seats that no longer have a hosted session."""
        with _data_lock:
            previous = {}
            changed = False
            for raw_user_id, hosted in hosted_by_user.items():
                user_id = int(raw_user_id)
                info = user_data.get(user_id)
                subscription = info.get('subscription') if isinstance(info, dict) else None
                if not isinstance(subscription, dict) or subscription.get('quota') is None:
                    continue
                allowed = {
                    ''.join(char for char in str(item) if char.isdigit())
                    for item in hosted
                }
                normalized = []
                for item in subscription.get('selected_accounts') or []:
                    digits = ''.join(char for char in str(item) if char.isdigit())
                    if digits in allowed and digits not in normalized:
                        normalized.append(digits)
                if normalized != list(subscription.get('selected_accounts') or []):
                    previous[user_id] = copy.deepcopy(info)
                    subscription['selected_accounts'] = normalized
                    changed = True
            if not changed:
                return True
            if DataManager.save_user_data():
                return True
            for user_id, snapshot in previous.items():
                user_data[user_id] = snapshot
            return False
    
    @staticmethod
    def delete_subscription(user_id: int) -> bool:
        """Delete subscription entitlement while retaining the user's hosted accounts."""
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return False
        if user_id <= 0 or DataManager.is_admin(user_id):
            return False

        info = user_data.get(user_id)
        has_entitlement = isinstance(info, dict) and bool(info.get('subscription'))
        if not has_entitlement:
            return False

        previous = copy.deepcopy(info)
        try:
            info.pop('subscription', None)
            info['last_updated'] = datetime.now().isoformat()
            DataManager._set_subscription_index(user_id, datetime.now(), active=False)
            if not DataManager.save_user_data():
                raise OSError("failed to persist subscription deletion")
            logger.info("✅ 已删除用户订阅: %s", user_id)
            return True
        except Exception as error:
            user_data[user_id] = previous
            DataManager.rebuild_subscription_index()
            logger.error("❌ 删除用户订阅失败: %s", error)
            return False

    @staticmethod
    def delete_user_data(user_id: int) -> bool:
        """删除用户全部数据，并同步刷新订阅索引。"""
        missing = object()
        removed = missing
        try:
            removed = user_data.pop(user_id, missing)
            DataManager._set_subscription_index(user_id, datetime.now(), active=False)
            if removed is missing:
                return True
            if not DataManager.save_user_data():
                user_data[user_id] = removed
                DataManager.rebuild_subscription_index()
                return False
            logger.info(f"🧹 已删除用户数据: {user_id}")
            return True
        except Exception as e:
            if removed is not missing:
                user_data[user_id] = removed
                DataManager.rebuild_subscription_index()
            logger.error(f"❌ 删除用户数据失败: {str(e)}")
            return False
    
    @staticmethod
    def get_all_subscription_users() -> List[Dict]:
        """获取所有活跃订阅用户信息。"""
        subscription_users = []
        for user_id, user_info in user_data.items():
            # 只处理整数键的用户数据
            if isinstance(user_id, int) and DataManager.has_active_subscription(user_id):
                subscription = user_info.get('subscription') or {}
                expiry_date = datetime.fromisoformat(subscription['expires_at'])
                try:
                    starts_at = datetime.fromisoformat(subscription['starts_at'])
                    total_days = max(0, (expiry_date - starts_at).days)
                except (KeyError, TypeError, ValueError):
                    total_days = 0
                days_left = (expiry_date - datetime.now()).days
                subscription_users.append({
                    'user_id': user_id,
                    'expiry': expiry_date,
                    'days_left': days_left,
                    'total_days': total_days,
                    'added_date': subscription.get('starts_at', '未知')
                })
        return subscription_users
    
    @staticmethod
    def get_expiring_subscription_users(days_before: int = 3) -> List[Dict]:
        """获取即将到期的订阅用户。"""
        expiring_users: List[Dict] = []
        current_time = datetime.now()

        for user_id, expiry_date in DataManager.iter_subscription_users():
            days_left = (expiry_date - current_time).days
            if 0 <= days_left <= days_before:
                subscription = (user_data.get(user_id, {}).get('subscription') or {})
                try:
                    starts_at = datetime.fromisoformat(subscription['starts_at'])
                    total_days = max(0, (expiry_date - starts_at).days)
                except (KeyError, TypeError, ValueError):
                    total_days = 0
                expiring_users.append({
                    'user_id': user_id,
                    'expiry': expiry_date,
                    'days_left': days_left,
                    'total_days': total_days,
                })

        return expiring_users

    
    @staticmethod
    def set_expiry_reminder_days(days: int):
        """设置到期提醒天数"""
        try:
            if 'system_settings' not in user_data:
                user_data['system_settings'] = {}
            
            user_data['system_settings']['expiry_reminder_days'] = days
            if not DataManager.save_user_data():
                return False
            logger.info(f"✅ 已设置到期提醒天数: {days}天")
            return True
        except Exception as e:
            logger.error(f"❌ 设置到期提醒天数失败: {str(e)}")
            return False
    
    @staticmethod
    def get_expiry_reminder_days():
        """获取到期提醒天数"""
        if 'system_settings' in user_data:
            return user_data['system_settings'].get('expiry_reminder_days', 3)
        return 3

    @staticmethod
    def get_login_unlock_reminder_schedule() -> Dict:
        settings = user_data.get('system_settings') or {}
        stored = settings.get('login_unlock_reminder_schedule') or {}
        try:
            count = int(stored.get('count', 1))
            offsets = [int(value) for value in stored.get('offsets_seconds', [120])]
        except (AttributeError, TypeError, ValueError):
            return {'count': 1, 'offsets_seconds': [120]}
        if (
            count < 1 or count > 5 or len(offsets) != count
            or any(value <= 0 for value in offsets)
            or any(value > 43200 * 60 for value in offsets)
            or any(value % 60 for value in offsets[:-1])
            or (offsets and offsets[-1] >= 60 and offsets[-1] % 60)
            or any(left <= right for left, right in zip(offsets, offsets[1:]))
        ):
            return {'count': 1, 'offsets_seconds': [120]}
        return {'count': count, 'offsets_seconds': offsets}

    @staticmethod
    def set_login_unlock_reminder_schedule(count: int, offsets_seconds: List[int]) -> bool:
        try:
            count = int(count)
            offsets = [int(value) for value in offsets_seconds]
        except (TypeError, ValueError):
            return False
        if (
            count < 1 or count > 5 or len(offsets) != count
            or any(value <= 0 for value in offsets)
            or any(value > 43200 * 60 for value in offsets)
            or any(value % 60 for value in offsets[:-1])
            or (offsets and offsets[-1] >= 60 and offsets[-1] % 60)
            or any(left <= right for left, right in zip(offsets, offsets[1:]))
        ):
            return False
        previous = copy.deepcopy(user_data.get('system_settings'))
        settings = user_data.setdefault('system_settings', {})
        settings['login_unlock_reminder_schedule'] = {
            'count': count,
            'offsets_seconds': offsets,
        }
        if DataManager.save_user_data():
            return True
        if previous is None:
            user_data.pop('system_settings', None)
        else:
            user_data['system_settings'] = previous
        return False

    @staticmethod
    def get_login_unlock_reminders(user_id: int) -> Dict:
        info = user_data.get(int(user_id), {})
        records = info.get('login_unlock_reminders') if isinstance(info, dict) else None
        return copy.deepcopy(records) if isinstance(records, dict) else {}

    @staticmethod
    def set_login_unlock_reminders(user_id: int, records: Dict) -> bool:
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return False
        existed = user_id in user_data
        previous = copy.deepcopy(user_data.get(user_id)) if existed else None
        info = user_data.setdefault(user_id, {})
        if records:
            info['login_unlock_reminders'] = copy.deepcopy(records)
        else:
            info.pop('login_unlock_reminders', None)
        info['last_updated'] = datetime.now().isoformat()
        if DataManager.save_user_data():
            return True
        if existed:
            user_data[user_id] = previous
        else:
            user_data.pop(user_id, None)
        return False

    @staticmethod
    def iter_login_unlock_reminder_users() -> List[int]:
        return sorted(
            user_id for user_id, info in user_data.items()
            if isinstance(user_id, int)
            and isinstance(info, dict)
            and isinstance(info.get('login_unlock_reminders'), dict)
            and info['login_unlock_reminders']
        )

    @staticmethod
    def get_login_code_request_timestamps(user_id: int) -> List[float]:
        info = user_data.get(int(user_id), {})
        stored = (
            info.get('login_code_request_timestamps')
            if isinstance(info, dict) else None
        )
        if not isinstance(stored, list):
            return []
        timestamps = []
        for value in stored:
            try:
                timestamp = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(timestamp):
                timestamps.append(timestamp)
        return sorted(timestamps)

    @staticmethod
    def set_login_code_request_timestamps(
        user_id: int, timestamps: List[float]
    ) -> bool:
        try:
            user_id = int(user_id)
            normalized = sorted(
                float(value) for value in timestamps
                if math.isfinite(float(value))
            )
        except (TypeError, ValueError, OverflowError):
            return False

        existed = user_id in user_data
        previous = copy.deepcopy(user_data.get(user_id)) if existed else None
        info = user_data.setdefault(user_id, {})
        if normalized:
            info['login_code_request_timestamps'] = normalized
        else:
            info.pop('login_code_request_timestamps', None)

        if existed and info == previous:
            return True
        if not existed and not info:
            user_data.pop(user_id, None)
            return True
        if DataManager.save_user_data():
            return True
        if existed:
            user_data[user_id] = previous
        else:
            user_data.pop(user_id, None)
        return False

    @staticmethod
    def iter_login_code_request_rate_users() -> List[int]:
        return sorted(
            user_id for user_id, info in user_data.items()
            if isinstance(user_id, int)
            and isinstance(info, dict)
            and isinstance(info.get('login_code_request_timestamps'), list)
            and info['login_code_request_timestamps']
        )

    @staticmethod
    def was_expiry_reminder_sent(user_id: int, expiry: datetime) -> bool:
        subscription = (user_data.get(int(user_id), {}).get('subscription') or {})
        reminder = subscription.get('expiry_reminder') or {}
        return reminder.get('expires_at') == expiry.isoformat()

    @staticmethod
    def mark_expiry_reminder_sent(
        user_id: int, expiry: datetime, days_left: int
    ) -> bool:
        info = user_data.get(int(user_id), {})
        subscription = info.get('subscription')
        if not isinstance(subscription, dict):
            return False
        previous = copy.deepcopy(subscription.get('expiry_reminder'))
        subscription['expiry_reminder'] = {
            'expires_at': expiry.isoformat(),
            'days_left': int(days_left),
            'sent_at': datetime.now().isoformat(),
        }
        if DataManager.save_user_data():
            return True
        if previous is None:
            subscription.pop('expiry_reminder', None)
        else:
            subscription['expiry_reminder'] = previous
        return False

    @staticmethod
    def get_payment_orders() -> Dict:
        """获取独立文件中持久化的支付订单。"""
        return payment_orders

    @staticmethod
    def load_payment_orders() -> bool:
        """加载当前 schema 的独立支付订单文件。"""
        global payment_orders, payment_orders_load_succeeded
        payment_orders_load_succeeded = False
        try:
            file_exists = os.path.exists(PAYMENT_ORDERS_FILE)
            if file_exists:
                with open(PAYMENT_ORDERS_FILE, 'r', encoding='utf-8') as stream:
                    loaded = json.load(stream)
                if not isinstance(loaded, dict):
                    raise ValueError('支付订单文件顶层必须是对象')
                if loaded.get('schema_version') != PAYMENT_ORDERS_SCHEMA_VERSION:
                    raise UnsupportedSchemaError(
                        '支付订单 schema 不受支持，请先运行 migrate_runtime_data.py --apply'
                    )
                orders = loaded.get('orders')
                if not isinstance(orders, dict):
                    raise ValueError('支付订单文件缺少 orders 对象')
                payment_orders = orders
            else:
                payment_orders = {}
            payment_orders_load_succeeded = True
            if not file_exists and not DataManager.save_payment_orders(payment_orders):
                payment_orders_load_succeeded = False
                return False
            DataManager.recover_payment_fulfillments()
            logger.info(f"✅ 已加载 {len(payment_orders)} 个支付订单")
            return True
        except UnsupportedSchemaError as e:
            logger.error(f"❌ 加载支付订单失败: {str(e)}")
            payment_orders = {}
            payment_orders_load_succeeded = False
            return False
        except Exception as e:
            logger.error(f"❌ 加载支付订单失败: {str(e)}")
            if os.path.exists(PAYMENT_ORDERS_FILE):
                timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
                backup = f"{PAYMENT_ORDERS_FILE}.load-failed.{timestamp}.backup"
                try:
                    shutil.copy2(PAYMENT_ORDERS_FILE, backup)
                except Exception:
                    pass
            payment_orders = {}
            payment_orders_load_succeeded = False
            return False

    @staticmethod
    def save_payment_orders(orders: Dict) -> bool:
        """原子保存支付订单到独立 JSON 文件。"""
        global payment_orders
        tmp_path = None
        try:
            if not payment_orders_load_succeeded:
                logger.error("拒绝保存支付订单：订单文件尚未成功加载")
                return False
            payment_orders = orders if isinstance(orders, dict) else {}
            legacy_order_ids = [
                str(order_id)
                for order_id, order in payment_orders.items()
                if isinstance(order, dict) and order.get('type') == 'vip_purchase'
            ]
            if legacy_order_ids:
                raise ValueError(
                    '支付订单仍包含旧 vip_purchase 类型: '
                    + ', '.join(legacy_order_ids[:5])
                )
            data_dir = os.path.dirname(os.path.abspath(PAYMENT_ORDERS_FILE)) or '.'
            os.makedirs(data_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=os.path.basename(PAYMENT_ORDERS_FILE) + '.',
                suffix='.tmp', dir=data_dir, text=True
            )
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(
                    {
                        'schema_version': PAYMENT_ORDERS_SCHEMA_VERSION,
                        'orders': payment_orders,
                    },
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_path, PAYMENT_ORDERS_FILE)
            tmp_path = None
            return True
        except Exception as e:
            logger.error(f"❌ 保存支付订单失败: {str(e)}")
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return False

    @staticmethod
    def fulfill_subscription_payment(order_id: str, orders: Dict) -> bool:
        """Atomically apply a snapshotted subscription order and mark it fulfilled."""
        order = orders.get(order_id)
        if not order or order.get('processed') or order.get('type') != 'subscription_purchase':
            return False
        try:
            user_id = int(order['user_id'])
            plan_id = str(order['plan_id'])
            quota = order.get('quota')
            days = int(order.get('period_days', 30))
            if plan_id not in {'go', 'plus', 'pro'}:
                return False
            if plan_id == 'pro':
                quota = None
            else:
                quota = int(quota)
                if quota <= 0:
                    return False
            if str(order.get('coin', '')).upper() != 'USDT' or days <= 0:
                return False
            if Decimal(str(order.get('amount'))) <= 0:
                return False
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return False

        missing = object()
        existing_user = user_data.get(user_id, missing)
        previous_user = missing if existing_user is missing else copy.deepcopy(existing_user)
        previous_order = copy.deepcopy(order)
        try:
            if order.get('fulfillment_state') == 'applying':
                DataManager.recover_payment_fulfillments()
                return bool(order.get('processed'))
            if order.get('billing_mode') == 'prorated_upgrade':
                upgrade_snapshot = copy.deepcopy(order.get('upgrade_snapshot') or {})
                for segment in upgrade_snapshot.get('target_segments') or []:
                    segment['order_id'] = order_id
                if not DataManager.apply_prorated_upgrade(user_id, upgrade_snapshot):
                    order.update({
                        'needs_manual_review': True,
                        'manual_review_reason': 'subscription_state_changed',
                        'auto_check_stopped': True,
                    })
                    DataManager.save_payment_orders(orders)
                    logger.error(
                        '差价升级订单已付款但源订阅状态发生变化，转人工处理: %s', order_id
                    )
                    return False
            elif not DataManager.apply_subscription(
                user_id, plan_id, quota, days, validate_catalog=False,
                billing_price=(
                    order.get('effective_monthly_price')
                    or order.get('catalog_price') or order.get('amount')
                ),
                order_id=order_id,
            ):
                return False
            updated_user = copy.deepcopy(user_data[user_id])
            order.update({
                'fulfillment_state': 'applying',
                'fulfillment_user_id': user_id,
                'fulfillment_user': updated_user,
            })
            if not DataManager.save_payment_orders(orders):
                raise RuntimeError('payment order prepare save failed')
            if not DataManager.save_user_data():
                raise RuntimeError('subscription user save failed')
            order.update({
                'processed': True, 'status': 'paid',
                'fulfilled_time': datetime.now().timestamp(),
            })
            order.pop('fulfillment_state', None)
            order.pop('fulfillment_user_id', None)
            order.pop('fulfillment_user', None)
            if not DataManager.save_payment_orders(orders):
                logger.error('订阅已发放，订单完成状态将在重启后恢复: %s', order_id)
            return True
        except Exception as error:
            if previous_user is missing:
                user_data.pop(user_id, None)
            else:
                user_data[user_id] = previous_user
            order.clear()
            order.update(previous_order)
            DataManager.rebuild_subscription_index()
            logger.error('原子处理订阅支付失败: 订单 %s: %s', order_id, error)
            return False

    @staticmethod
    def recover_payment_fulfillments() -> bool:
        """Complete crash-interrupted VIP grants recorded in the payment file."""
        changed_users = False
        recovering = []
        for order_id, order in payment_orders.items():
            if order.get('fulfillment_state') != 'applying':
                continue
            user_id = order.get('fulfillment_user_id')
            target = order.get('fulfillment_user')
            if not user_id or not isinstance(target, dict):
                logger.error("支付订单恢复信息损坏: %s", order_id)
                continue
            user_id = int(user_id)
            current = user_data.setdefault(user_id, {})
            current_expiry = (current.get('subscription') or {}).get('expires_at', '')
            target_expiry = (target.get('subscription') or {}).get('expires_at', '')
            if not current_expiry or current_expiry <= target_expiry:
                if any(current.get(key) != value for key, value in target.items()):
                    current.update(target)
                    changed_users = True
            recovering.append((order_id, order))

        if changed_users:
            DataManager.rebuild_subscription_index()
            if not DataManager.save_user_data():
                return False
        if recovering:
            for order_id, order in recovering:
                order.update({'processed': True, 'status': 'paid'})
                order.pop('fulfillment_state', None)
                order.pop('fulfillment_user_id', None)
                order.pop('fulfillment_user', None)
                logger.info("✅ 已恢复中断的支付发放: %s", order_id)
            return DataManager.save_payment_orders(payment_orders)
        return True
    
def _synchronized_static_method(method):
    def synchronized(*args, **kwargs):
        with _data_lock:
            return method(*args, **kwargs)
    synchronized.__name__ = method.__name__
    synchronized.__doc__ = method.__doc__
    return staticmethod(synchronized)


for _method_name in (
    "load_user_data", "save_user_data", "load_payment_orders",
    "grant_subscription", "delete_subscription",
    "delete_user_data",
    "set_expiry_reminder_days", "save_payment_orders",
    "set_login_unlock_reminder_schedule", "set_login_unlock_reminders",
    "set_login_code_request_timestamps",
    "set_subscription_catalog", "set_subscription_periods", "apply_subscription", "fulfill_subscription_payment",
    "quote_subscription_upgrade", "apply_prorated_upgrade",
    "set_selected_accounts",
    "initialize_user_language", "set_user_language", "set_user_timezone",
    "activate_due_subscriptions", "mark_expiry_reminder_sent",
    "recover_payment_fulfillments",
):
    setattr(DataManager, _method_name, _synchronized_static_method(getattr(DataManager, _method_name)))
