"""Explicit optional platform composition; construction never performs I/O.

Only the isolated runtime imports this module. The working SPN bot is unchanged.
Delivery workers have their own default-off switches, separate from viewing or
editing management screens. No import enables payments, transfers or messages.
"""


def build_platform_extensions(runtime):
    settings = runtime.settings
    if settings.mode != "staging":
        return {}
    kwargs, hooks = {}, []
    allowed = settings.allow_staging_network and settings.allow_staging_writes
    sender = getattr(runtime.notifications, "sender", None)
    if (
        allowed
        and any(
            (
                settings.enable_marketing_delivery,
                settings.enable_support_delivery,
                settings.enable_finance_notifications,
            )
        )
        and sender is None
    ):
        from services.partner_notifications import TelegramNotificationAdapter

        sender = TelegramNotificationAdapter()
    if settings.management_enabled:
        from services.partner_management import (
            PartnerManagementStore,
            PartnerMarketingDeliveryService,
            PartnerSupportDeliveryService,
        )

        management = PartnerManagementStore(
            runtime.pool_getter,
            delivery_enabled=allowed and settings.enable_marketing_delivery,
            support_delivery_enabled=allowed and settings.enable_support_delivery,
        )
        kwargs["management_store"] = management
        marketing = PartnerMarketingDeliveryService(
            management,
            sender,
            vault=runtime.vault,
            enabled=allowed and settings.enable_marketing_delivery,
        )
        support = PartnerSupportDeliveryService(
            management,
            sender,
            vault=runtime.vault,
            subscription_pages=runtime.subscriptions,
            enabled=allowed and settings.enable_support_delivery,
        )

        async def deliver_marketing():
            return await marketing.recover_once(limit=settings.pass_limit)

        async def deliver_support():
            return await support.recover_once(limit=settings.pass_limit)

        hooks.extend(
            (
                ("marketing_delivery", deliver_marketing, marketing.ready),
                ("support_delivery", deliver_support, support.ready),
            )
        )
    if settings.finance_enabled:
        from services.partner_finance import FinanceVault, PartnerFinanceStore
        from services.partner_finance_notifications import (
            FinanceNotificationStore,
            FinanceNotificationService,
        )

        kwargs["finance_store"] = PartnerFinanceStore(
            runtime.pool_getter,
            vault=FinanceVault(settings.encryption_keys),
            operator_ids=settings.operator_ids,
            admin_recipient_id=settings.admin_recipient_id,
            enabled=True,
        )
        kwargs["operator_ids"] = settings.operator_ids
        outbox = FinanceNotificationStore(
            runtime.pool_getter,
            admin_recipient_id=settings.admin_recipient_id,
            enabled=allowed and settings.enable_finance_notifications,
        )
        finance_delivery = FinanceNotificationService(
            outbox,
            sender,
            manager_bot_token=settings.manager_bot_token,
            enabled=allowed and settings.enable_finance_notifications,
        )

        async def deliver_finance():
            return await finance_delivery.recover_once(limit=settings.pass_limit)

        hooks.append(("finance_delivery", deliver_finance, finance_delivery.ready))
    if settings.branding_enabled:
        from services.partner_brand_assets import PartnerBrandAssetStore

        kwargs["brand_asset_store"] = PartnerBrandAssetStore(
            runtime.pool_getter, enabled=True
        )
    result = {"core_kwargs": kwargs, "routers": [], "worker_hooks": hooks}
    if getattr(settings, "enable_public_checkout", False):
        from services.partner_public_checkout import (
            PartnerPublicCheckoutStore,
            PartnerPublicCheckoutService,
        )
        from partner_public_checkout_web import create_router

        public_checkout = PartnerPublicCheckoutService(
            runtime.subscriptions,
            runtime.checkout,
            PartnerPublicCheckoutStore(runtime.pool_getter, enabled=True),
            enabled=True,
        )
        result["public_checkout"] = public_checkout
        result["routers"].append(create_router(public_checkout, enabled=True))
    return result
