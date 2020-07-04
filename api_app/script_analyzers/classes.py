import traceback
import time
import logging
import requests
from abc import ABC, abstractmethod

from django.utils import timezone
from django.db import transaction

from api_app import models
from api_app.utilities import get_now
from api_app.exceptions import (
    AnalyzerRunException,
    AnalyzerConfigurationException,
    AnalyzerRunNotImplemented,
    AlreadyFailedJobException,
)

logger = logging.getLogger(__name__)


def set_job_status(job_id, status, errors=None):
    message = f"setting job_id {job_id} to status {status}"
    if status == "failed":
        logger.error(message)
    else:
        logger.info(message)
    job_object = models.Job.object_by_job_id(job_id)
    if errors:
        job_object.errors.extend(errors)
    job_object.status = status
    job_object.save()


class BaseAnalyzerMixin(ABC):
    """
    Abstract Base class for Analyzers.
    Never inherit from this branch,
    always use either one of ObservableAnalyzer or FileAnalyzer classes.
    """

    __job_id: int
    analyzer_name: str

    @property
    def job_id(self):
        return self.__job_id

    @abstractmethod
    def before_run(self):
        """
        function called directly before run function.
        """

    @abstractmethod
    def run(self):
        # this should be overwritten in
        # child class
        raise AnalyzerRunNotImplemented(self.analyzer_name)

    @abstractmethod
    def after_run(self):
        """
        function called after run function.
        """

    def set_config(self, additional_config_params):
        """
        function to parse additional_config_params.
        verify params, API keys, etc.
        In most cases, this would be overwritten.
        """

    def start(self):
        """
        Entrypoint function to execute the analyzer.
        calls `before_run`, `run`, `after_run`
        in that order with exception handling.
        """
        self.before_run()
        try:
            self.report = self.get_basic_report_template(self.analyzer_name)
            result = self.run()
            self.report["report"] = result
        except (AnalyzerConfigurationException, AnalyzerRunException) as e:
            self._handle_analyzer_exception(e)
        except Exception as e:
            self._handle_base_exception(e)
        else:
            self.report["success"] = True

        # add process time
        self.report["process_time"] = time.time() - self.report["started_time"]
        self.set_report_and_cleanup(self.job_id, self.report)

        self.after_run()

        return self.report

    def _handle_analyzer_exception(self, err):
        error_message = (
            f"job_id:{self.job_id}, analyzer: '{self.analyzer_name}'."
            f" Analyzer error: '{err}'"
        )
        logger.error(error_message)
        self.report["errors"].append(error_message)
        self.report["success"] = False

    def _handle_base_exception(self, err):
        traceback.print_exc()
        error_message = (
            f"job_id:{self.job_id}, analyzer:'{self.analyzer_name}'."
            f" Unexpected error: '{err}'"
        )
        logger.exception(error_message)
        self.report["errors"].append(str(err))
        self.report["success"] = False

    @staticmethod
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

    @staticmethod
    def set_report_and_cleanup(job_id, report):
        analyzer_name = report.get("name", "")
        logger.info(
            f"start set_report_and_cleanup for job_id:{job_id},"
            f" analyzer:{analyzer_name}"
        )
        job_object = None

        try:
            with transaction.atomic():
                job_object = models.Job.object_by_job_id(job_id, transaction=True)
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

    def __init__(self, analyzer_name, job_id, additional_config_params):
        self.analyzer_name = analyzer_name
        self.__job_id = job_id
        self.set_config(additional_config_params)


class ObservableAnalyzer(BaseAnalyzerMixin):
    """
    Abstract class for Observable Analyzers.
    Inherit from this branch when defining a IP, URL or domain analyzer.
    Need to overrwrite `set_config(self, additional_config_params)`
     and `run(self)` functions.
    """

    observable_name: str
    observable_classification: str

    def __init__(
        self,
        analyzer_name,
        job_id,
        obs_name,
        obs_classification,
        additional_config_params,
    ):
        self.observable_name = obs_name
        self.observable_classification = obs_classification
        super().__init__(analyzer_name, job_id, additional_config_params)

    def before_run(self):
        logger.info(
            "started analyzer: {}, job_id: {}, observable: {}"
            "".format(self.analyzer_name, self.job_id, self.observable_name)
        )

    def after_run(self):
        logger.info(
            f"ended analyzer: {self.analyzer_name}, job_id: {self.job_id},"
            f"observable: {self.observable_name}"
        )


class FileAnalyzer(BaseAnalyzerMixin):
    """
    Abstract class for File Analyzers.
    Inherit from this branch when defining a file analyzer.
    Need to overrwrite `set_config(self, additional_config_params)`
     and `run(self)` functions.
    """

    md5: str
    filepath: str
    filename: str

    def __init__(
        self, analyzer_name, job_id, fpath, fname, md5, additional_config_params
    ):
        self.md5 = md5
        self.filepath = fpath
        self.filename = fname
        super().__init__(analyzer_name, job_id, additional_config_params)

    def before_run(self):
        logger.info(f"started analyzer: {self.analyzer_name}, job_id: {self.job_id}")

    def after_run(self):
        logger.info(
            f"ended analyzer: {self.analyzer_name}, job_id: {self.job_id},"
            f"md5: {self.md5} ,filename: {self.filename}"
        )


class DockerBasedAnalyzer(ABC):
    """
    Abstract class for a docker based analyzer (integration).
    Inherit this branch along with either one of ObservableAnalyzer or FileAnalyzer
    when defining a docker based analyzer.
    See `peframe.py` for example.

    :param max_tries: int
        maximum no. of tries when HTTP polling for result.
    :param poll_distance: int
        interval between HTTP polling.
    """

    max_tries: int
    poll_distance: int

    @staticmethod
    def _check_status_code(name, req):
        # handle errors manually
        if req.status_code == 404:
            raise AnalyzerRunException(f"{name} docker container is not running.")
        if req.status_code == 400:
            err = req.json()["error"]
            raise AnalyzerRunException(err)
        if req.status_code == 500:
            raise AnalyzerRunException(
                f"Internal Server Error in {name} docker container"
            )
        # just in case couldn't catch the error manually
        req.raise_for_status()

        return True

    @staticmethod
    def _query_for_result(url, key):
        headers = {"Accept": "application/json"}
        resp = requests.get(f"{url}?key={key}", headers=headers)
        data = resp.json()
        return resp.status_code, data

    def _poll_for_result(self, req_key):
        got_result = False
        for chance in range(self.max_tries):
            time.sleep(self.poll_distance)
            logger.info(
                f"{self.analyzer_name} polling. Try n:{chance+1}, job_id:{self.job_id}."
                "Starting the query"
            )
            try:
                status_code, json_data = self._query_for_result(self.url, req_key)
            except requests.RequestException as e:
                raise AnalyzerRunException(e)
            analysis_status = json_data.get("status", None)
            if analysis_status in ["success", "reported_with_fails", "failed"]:
                got_result = True
                break
            elif status_code == 404:
                pass
            else:
                logger.info(
                    f"{self.analyzer_name} polling."
                    f" Try n:{chance+1}, job_id:{self.job_id}, status:{analysis_status}"
                )

        if not got_result:
            raise AnalyzerRunException(
                f"max {self.analyzer_name} polls tried without getting any result."
                f"job_id:{self.job_id}"
            )
        return json_data
