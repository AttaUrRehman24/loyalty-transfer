"""Partner adapters: one file per partner API.

To add a partner: create one module here that defines a class decorated with
`@register_adapter("<name>")`, then insert a `programs` row whose `adapter` column is
`<name>`. `registry.load_adapter_modules` imports every module in this package at
startup, so no other file needs to change.
"""
