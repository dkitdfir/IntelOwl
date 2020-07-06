import time
import logging

from django.db import transaction
from django.utils import timezone

from api_app.models import Job
from api_app.helpers import get_now
from api_app.exceptions import AlreadyFailedJobException


logger = logging.getLogger(__name__)


def set_report_and_cleanup(job_id, report):
    analyzer_name = report.get("name", "")
    logger.info(
        f"start set_report_and_cleanup for job_id:{job_id},"
        f" analyzer:{analyzer_name}"
    )
    job_object = None

    try:
        with transaction.atomic():
            job_object = Job.object_by_job_id(job_id, transaction=True)
            job_object.analysis_reports.append(report)
            job_object.save(update_fields=["analysis_reports"])
            if job_object.status == "failed":
                raise AlreadyFailedJobException()

        num_analysis_reports = len(job_object.analysis_reports)
        num_analyzers_to_execute = len(job_object.analyzers_to_execute)
        logger.info(
            f"job_id:{job_id}, analyzer {analyzer_name}, "
            f"num analysis reports:{num_analysis_reports}, "
            f"num analyzer to execute:{num_analyzers_to_execute}"
        )

        # check if it was the last analysis...
        # ..In case, set the analysis as "reported" or "failed"
        if num_analysis_reports == num_analyzers_to_execute:
            status_to_set = "reported_without_fails"
            # set status "failed" in case all analyzers failed
            failed_analyzers = 0
            for analysis_report in job_object.analysis_reports:
                if not analysis_report.get("success", False):
                    failed_analyzers += 1
            if failed_analyzers == num_analysis_reports:
                status_to_set = "failed"
            elif failed_analyzers >= 1:
                status_to_set = "reported_with_fails"
            set_job_status(job_id, status_to_set)
            job_object.finished_analysis_time = get_now()
            job_object.save(update_fields=["finished_analysis_time"])

    except AlreadyFailedJobException:
        logger.error(
            f"job_id {job_id} status failed. Do not process the report {report}"
        )

    except Exception as e:
        logger.exception(f"job_id: {job_id}, Error: {e}")
        set_job_status(job_id, "failed", errors=[str(e)])
        job_object.finished_analysis_time = get_now()
        job_object.save(update_fields=["finished_analysis_time"])


def get_basic_report_template(analyzer_name):
    return {
        "name": analyzer_name,
        "success": False,
        "report": {},
        "errors": [],
        "process_time": 0,
        "started_time": time.time(),
        "started_time_str": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_filepath_filename(job_id):
    # this function allows to minimize access to the database
    # in this way the analyzers could not touch the DB until the end of the analysis
    job_object = Job.object_by_job_id(job_id)

    filename = job_object.file_name

    file_path = job_object.file.path

    return file_path, filename


def get_observable_data(job_id):
    job_object = Job.object_by_job_id(job_id)

    observable_name = job_object.observable_name
    observable_classification = job_object.observable_classification

    return observable_name, observable_classification


def set_job_status(job_id, status, errors=None):
    message = f"setting job_id {job_id} to status {status}"
    if status == "failed":
        logger.error(message)
    else:
        logger.info(message)
    job_object = Job.object_by_job_id(job_id)
    if errors:
        job_object.errors.extend(errors)
    job_object.status = status
    job_object.save()


def set_failed_analyzer(analyzer_name, job_id, error_message):
    logger.info(
        f"setting analyzer {analyzer_name} of job_id {job_id} as failed."
        f" Error message:{error_message}"
    )
    report = get_basic_report_template(analyzer_name)
    report["errors"].append(error_message)
    set_report_and_cleanup(job_id, report)
