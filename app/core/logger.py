import logging
import json
import sys


class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            # 如果有 extra 字段也能自动带进去
        }
        if hasattr(record, 'trace_id'):
            log_obj['trace_id'] = record.trace_id

        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_obj, ensure_ascii=False)


def setup_logger():
    logger = logging.getLogger("dbops_agent")
    logger.setLevel(logging.INFO)

    # 避免重复添加 handler
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)

    return logger


# 全局单例
logger = setup_logger()