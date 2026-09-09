from agent_bridge.models import ConversationType, UnifiedMessage


def test_binding_key_is_channel_scoped() -> None:
    message = UnifiedMessage(
        channel="wechat",
        channel_account_id="account-1",
        conversation_id="group-1",
        conversation_type=ConversationType.GROUP,
        sender_id="user-1",
        message_id="message-1",
        content="hello",
    )

    assert message.binding_key == ("wechat", "account-1", "group-1")

