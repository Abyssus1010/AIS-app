"""Process-wide shared state. Kept in its own module (rather than on
ais_client or worker) so both can import it without a circular dependency."""

IN_FLIGHT: set[int] = set()
