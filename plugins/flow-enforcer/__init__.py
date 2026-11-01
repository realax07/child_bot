# -*- coding: utf-8 -*-
"""flow-enforcer — Hermes-плагин (J43): pre_tool_call-энфорсер делегаций.

Точка входа плагина: register(ctx) регистрирует pre_tool_call-хук
flow_enforce (см. enforcer.py — вся логика там, чтобы тесты импортировали
ядро без PluginContext).
"""
from enforcer import flow_enforce, register  # noqa: F401
