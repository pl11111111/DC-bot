"""Shared order lifecycle policy (channel retention is a separate decision)."""
TERMINAL = ('completed', 'cancelled', 'refunded', 'test_closed', 'manual_refunded', 'expired')
TERMINAL_SQL = '(' + ','.join("'" + state + "'" for state in TERMINAL) + ')'
AUTO_CLEANUP = tuple(state for state in TERMINAL if state != 'manual_refunded')
IDLE_SECONDS = {'pending': 1800, 'confirmed': 900}

