"""Talaria row 17: benign mcp streamable-HTTP GET-stream chatter never reaches errors.log."""
import logging

import hermes_logging


def _emit(logger, level, msg):
    rec = logger.makeRecord(logger.name, level, __file__, 1, msg, (), None)
    return all(f.filter(rec) for f in logger.filters)


def test_benign_endpoint_and_reconnect_lines_are_dropped():
    hermes_logging._quiet_noisy_loggers()
    lg = logging.getLogger("mcp.client.streamable_http")
    assert not _emit(lg, logging.WARNING, "Unknown SSE event: endpoint")
    assert not _emit(lg, logging.INFO, "GET stream disconnected, reconnecting in 1000ms...")


def test_other_warnings_from_the_same_logger_pass():
    hermes_logging._quiet_noisy_loggers()
    lg = logging.getLogger("mcp.client.streamable_http")
    assert _emit(lg, logging.WARNING, "Unknown SSE event: something_new")
    assert _emit(lg, logging.ERROR, "Error parsing SSE message")
    # idempotent: a second setup call does not stack filters
    hermes_logging._quiet_noisy_loggers()
    assert sum(isinstance(f, hermes_logging._BenignMcpStreamFilter) for f in lg.filters) == 1
