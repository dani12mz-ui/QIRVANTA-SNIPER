"""Add management hooks to the immutable native adapter during experimental build."""


def _replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError(f'Expected exactly one management anchor: {old}')
    return source.replace(old, new, 1)


def patch_management(source: str, component: str | None = None) -> str:
    """Patch native-only execution. The caller owns component insertion."""
    if component is not None and source.count(component.strip()) != 1:
        raise ValueError('Expected the management component exactly once')
    source = _replace_once(source, 'var float nativeLegQty = na\n', '''var float nativeLegQty = na
var float nativeLeg2Qty = na
var float nativeLeg3Qty = na
var float nativeTvBoundary = na
var bool nativeTvTP1 = false
var bool nativeTvTP2 = false
var string nativeTvStopStatus = ""
''')
    source = _replace_once(source, 'bool nativeAdmittedNow = false\n', '''[tvPreparedPlan, tvManagementReason, tvOrderLimit, tvFrozenBoundary] = f_tvManagePlan(selectedNativePlan, nativeUnitQty)
[tvPart1, tvPart2, tvPart3] = f_tvManagementQty(nativeUnitQty)
bool nativeAdmittedNow = false
''')
    for price, stamp in (('nativeEntry', 'strategy.opentrades.entry_time(0)'),
                         ('fillEntry', 'strategy.closedtrades.entry_time(j)')):
        args = f'(nativePlan, {price}, {stamp})'
        source = _replace_once(source, 'f_openRejection' + args, 'f_tvNativeOpenRejection' + args)
    source = _replace_once(source, '"Native market fill; няма pre-open rejection"',
                           'tvLimitEntry ? "Симулиран лимитен вход" : "Native market fill; няма pre-open rejection"')
    source = _replace_once(source, '"Native е изпълнил гап, който PAPER може да отхвърли"',
                           'tvManagement ? "Симулираният вход е извън замразената геометрия" : "Native е изпълнил гап, който PAPER може да отхвърли"')
    source = _replace_once(source, '                    if note == "TP1" and not na(nativeRisk)\n', '''                    if tvManagement and note == "TP1"
                        nativeTvTP1 := true
                    if note == "TP1" and not na(nativeRisk) and tvUsesBaselineLock
''')
    source = _replace_once(source, '                    nativeLeg2Closed := true\n', '''                    nativeLeg2Closed := true
                    if tvManagement and note == "TP2"
                        nativeTvTP2 := true
''')
    source = _replace_once(source,
                           '    if not na(nativePlan) and not nativeEntryObserved and bar_index >= nativePlan.expiresAfterBar\n',
                           '''    string tvPendingExplanation = not nativeEntryObserved ? f_tvPendingReason(nativePlan) : ""
    if not na(nativePlan) and not nativeEntryObserved and (bar_index >= nativePlan.expiresAfterBar or tvPendingExplanation != "")
''')
    source = _replace_once(source,
                           '        nativeLastStatus := "НЕИЗПЪЛНЕНА ЗАЯВКА · емулаторът не връща точната причина; провери капитал, маржин и количество"',
                           '        nativeLastStatus := tvPendingExplanation != "" ? "НЕИЗПЪЛНЕНА ЗАЯВКА · " + tvPendingExplanation : "НЕИЗПЪЛНЕНА ЗАЯВКА · емулаторът не връща точната причина; провери капитал, маржин и количество"')
    source = _replace_once(source, '    if not na(nativePlan) and strategy.position_size != 0\n        if nativeStopChanged\n',
                           '''    if not na(nativePlan) and strategy.position_size != 0
        if tvManagement and not nativeTimeOrder
            [tvNextStop, tvStopReason] = f_tvManagedStop(nativePlan, nativeEntry, nativeRisk, nativeStop, nativeTvBoundary, nativeTvTP1, nativeTvTP2)
            if tvStopReason != ""
                nativeStop := tvNextStop
                nativeStopChanged := true
                nativeTvStopStatus := tvStopReason
        if nativeStopChanged
''')
    anchor = '        if not nativeTimeOrder and not na(nativeFillBar) and bar_index - nativeFillBar >= nativeMaxHoldBars\n'
    source = _replace_once(source, anchor, '''        string tvScheduledExit = not nativeTimeOrder ? f_tvManagementCloseReason(nativePlan, nativeTvBoundary, nativeFillBar) : ""
        if tvScheduledExit != ""
            strategy.close(nativePlan.id, comment=tvScheduledExit, alert_message=f_nativeMessage(nativePlan, nativeCampaignQty, "NATIVE_EXIT_RESEARCH", tvScheduledExit, na), disable_alert=not (alertsEnabled and alertNativeFills))
            nativeTimeOrder := true
''' + anchor)
    anchor = '    // A confirmed technical candidate is not automatically an admitted plan or an executed trade.\n'
    source = _replace_once(source, anchor, '''    if nativeAdmissionReason == "" and tvManagementReason != ""
        nativeAdmissionReason := tvManagementReason
''' + anchor)
    source = _replace_once(source, '        nativePlan := selectedNativePlan.copy()\n',
                           '        nativePlan := tvPreparedPlan.copy()\n')
    source = _replace_once(source, '        nativeLegQty := nativeUnitQty\n', '''        nativeLegQty := tvPart1
        nativeLeg2Qty := tvPart2
        nativeLeg3Qty := tvPart3
        nativeTvBoundary := tvFrozenBoundary
        nativeTvTP1 := false
        nativeTvTP2 := false
        nativeTvStopStatus := ""
''')
    source = _replace_once(source, 'nativeCampaignQty := 3.0 * nativeLegQty',
                           'nativeCampaignQty := tvManagement ? 3.0 * nativeUnitQty : 3.0 * nativeLegQty')
    for leg in (2, 3):
        old = f'strategy.exit(nativePlan.id + "|T{leg}", from_entry=nativePlan.id, qty=nativeLegQty,'
        if source.count(old) != 2:
            raise ValueError(f'Expected two quantity anchors for leg {leg}')
        source = source.replace(old, old.replace('nativeLegQty', f'nativeLeg{leg}Qty'))
    source = _replace_once(source, 'qty=nativeCampaignQty, comment=f_model(nativePlan.family),',
                           'qty=nativeCampaignQty, limit=tvOrderLimit, comment=f_model(nativePlan.family),')
    source = _replace_once(source, '// Three equal reservations; each limit/stop pair shares only its own reduce OCA group.',
                           '// Three reservations; each limit/stop pair shares only its own reduce OCA group.')
    source = _replace_once(source, '        nativeLastStatus := "ЗАЯВЕН ПЛАН · чака следващ възможен тик"',
                           '        nativeLastStatus := tvLimitEntry ? "ЗАЯВЕН ЛИМИТ · чака цена" : "ЗАЯВЕН ПЛАН · чака следващ възможен тик"')
    return source
