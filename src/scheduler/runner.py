"""APScheduler 后台常驻: 按 config.yaml 里的 cron 表达式调度所有任务。"""
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from ..config import CFG
from ..utils import setup_logger
from ..pipeline import run_ingest_all, run_factors_all, run_reviews_all, run_report


log = setup_logger("scheduler", CFG["output"]["log_level"])


def start() -> None:
    sched = BlockingScheduler(timezone="UTC")
    sch = CFG["schedule"]
    sched.add_job(run_ingest_all, CronTrigger.from_crontab(sch["ingest_high_freq"]),
                  id="ingest_high_freq", max_instances=1, coalesce=True)
    sched.add_job(run_factors_all, CronTrigger.from_crontab(sch["compute_factors"]),
                  id="compute_factors", max_instances=1, coalesce=True)
    sched.add_job(run_reviews_all, CronTrigger.from_crontab(sch["review"]),
                  id="review", max_instances=1, coalesce=True)
    sched.add_job(run_report, CronTrigger.from_crontab(sch["daily_report"]),
                  id="daily_report", max_instances=1, coalesce=True)

    log.info("Scheduler started. Jobs: %s", [j.id for j in sched.get_jobs()])
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
