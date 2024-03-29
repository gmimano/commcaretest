from datetime import datetime, timedelta
from celery.schedules import crontab
from celery.task import periodic_task, task
from celery.utils.log import get_task_logger
from corehq.apps.domain.calculations import CALC_FNS, _all_domain_stats, total_distinct_users
from corehq.apps.hqadmin.escheck import check_es_cluster_health, CLUSTER_HEALTH, check_case_es_index, check_xform_es_index, check_reportcase_es_index, check_reportxform_es_index
from corehq.apps.reports.export import save_metadata_export_to_tempfile
from corehq.apps.reports.models import (ReportNotification,
    UnsupportedScheduledReportError, HQGroupExportConfiguration)
from corehq.elastic import get_es, ES_URLS, stream_es_query, es_query
from corehq.pillows.mappings.app_mapping import APP_INDEX
from corehq.pillows.mappings.domain_mapping import DOMAIN_INDEX
from couchexport.groupexports import export_for_group
from dimagi.utils.couch.database import get_db
from dimagi.utils.logging import notify_exception
from couchexport.tasks import cache_file_to_be_served
from django.conf import settings

logging = get_task_logger(__name__)

@periodic_task(run_every=crontab(hour="*/6", minute="0", day_of_week="*"), queue=getattr(settings, 'CELERY_PERIODIC_QUEUE','celery'))
def check_es_index():
    """
    Verify that the Case and soon to be added XForm Elastic indices are up to date with what's in couch

    This code is also called in the HQ admin page as well
    """

    es_status = {}
    es_status.update(check_es_cluster_health())

    es_status.update(check_case_es_index())
    es_status.update(check_xform_es_index())

    es_status.update(check_reportcase_es_index())
    es_status.update(check_reportxform_es_index())

    do_notify = False
    message = []
    if es_status[CLUSTER_HEALTH] == 'red':
        do_notify = True
        message.append("Cluster health is red - something is up with the ES machine")

    for index in es_status.keys():
        if index == CLUSTER_HEALTH:
            continue
        pillow_status = es_status[index]
        if not pillow_status['status']:
            do_notify = True
            message.append(
                "Elasticsearch %s Index Issue: %s" % (index, es_status[index]['message']))

    if do_notify:
        message.append("This alert can give false alarms due to timing lag, so please double check https://www.commcarehq.org/hq/admin/system/ and the Elasticsarch Status section to make sure.")
        notify_exception(None, message='\n'.join(message))


@task
def send_report(notification_id):
    notification = ReportNotification.get(notification_id)
    try:
        notification.send()
    except UnsupportedScheduledReportError:
        pass

@task
def create_metadata_export(download_id, domain, format, filename, datespan=None, user_ids=None):
    tmp_path = save_metadata_export_to_tempfile(domain, datespan, user_ids)

    class FakeCheckpoint(object):
        # for some silly reason the export cache function wants an object that looks like this
        # so just hack around it with this stub class rather than do a larger rewrite

        def __init__(self, domain):
            self.domain = domain

        @property
        def get_id(self):
            return '%s-form-metadata' % self.domain

    return cache_file_to_be_served(tmp_path, FakeCheckpoint(domain), download_id, format, filename)

@periodic_task(run_every=crontab(hour="*", minute="0", day_of_week="*"), queue=getattr(settings, 'CELERY_PERIODIC_QUEUE','celery'))
def daily_reports():    
    # this should get called every hour by celery
    reps = ReportNotification.view("reportconfig/all_notifications",
                                   startkey=["daily", datetime.utcnow().hour],
                                   endkey=["daily", datetime.utcnow().hour, {}],
                                   reduce=False,
                                   include_docs=True).all()
    for rep in reps:
        send_report.delay(rep._id)

@periodic_task(run_every=crontab(hour="*", minute="1", day_of_week="*"), queue=getattr(settings, 'CELERY_PERIODIC_QUEUE','celery'))
def weekly_reports():    
    # this should get called every hour by celery
    now = datetime.utcnow()
    reps = ReportNotification.view("reportconfig/all_notifications",
                                   key=["weekly", now.hour, now.weekday()],
                                   reduce=False,
                                   include_docs=True).all()
    for rep in reps:
        send_report.delay(rep._id)

@periodic_task(run_every=crontab(hour="*", minute="1", day_of_week="*"), queue=getattr(settings, 'CELERY_PERIODIC_QUEUE','celery'))
def monthly_reports():
    now = datetime.utcnow()
    reps = ReportNotification.view("reportconfig/all_notifications",
                                   key=["monthly", now.hour, now.day],
                                   reduce=False,
                                   include_docs=True).all()
    for rep in reps:
        send_report.delay(rep._id)

@periodic_task(run_every=crontab(hour=[22], minute="0", day_of_week="*"), queue=getattr(settings, 'CELERY_PERIODIC_QUEUE','celery'))
def saved_exports():    
    for group_config in HQGroupExportConfiguration.view("groupexport/by_domain", reduce=False,
                                                        include_docs=True).all():
        export_for_group(group_config, "couch")

@periodic_task(run_every=crontab(hour="12, 22", minute="0", day_of_week="*"), queue=getattr(settings, 'CELERY_PERIODIC_QUEUE','celery'))
def update_calculated_properties():
    es = get_es()

    q = {"filter": {"and": [
        {"term": {"doc_type": "Domain"}},
        {"term": {"is_snapshot": False}}
    ]}}
    results = stream_es_query(q=q, es_url=ES_URLS["domains"], size=999999, chunksize=500, fields=["name"])
    all_stats = _all_domain_stats()
    for r in results:
        dom = r["fields"]["name"]
        calced_props = {
            "cp_n_web_users": int(all_stats["web_users"][dom]),
            "cp_n_active_cc_users": int(CALC_FNS["mobile_users"](dom)),
            "cp_n_cc_users": int(all_stats["commcare_users"][dom]),
            "cp_n_active_cases": int(CALC_FNS["cases_in_last"](dom, 120)),
            "cp_n_users_submitted_form": total_distinct_users([dom]),
            "cp_n_inactive_cases": int(CALC_FNS["inactive_cases_in_last"](dom, 120)),
            "cp_n_60_day_cases": int(CALC_FNS["cases_in_last"](dom, 60)),
            "cp_n_cases": int(all_stats["cases"][dom]),
            "cp_n_forms": int(all_stats["forms"][dom]),
            "cp_first_form": CALC_FNS["first_form_submission"](dom, False),
            "cp_last_form": CALC_FNS["last_form_submission"](dom, False),
            "cp_is_active": CALC_FNS["active"](dom),
            "cp_has_app": CALC_FNS["has_app"](dom),
        }
        if calced_props['cp_first_form'] == 'No forms':
            del calced_props['cp_first_form']
            del calced_props['cp_last_form']
        es.post("%s/hqdomain/%s/_update" % (DOMAIN_INDEX, r["_id"]), data={"doc": calced_props})

DATE_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
def is_app_active(app_id, domain):
    now = datetime.now()
    then = (now - timedelta(days=30)).strftime(DATE_FORMAT)
    now = now.strftime(DATE_FORMAT)

    key = ['submission app', domain, app_id]
    row = get_db().view("reports_forms/all_forms", startkey=key+[then], endkey=key+[now]).all()
    return True if row else False

@periodic_task(run_every=crontab(hour="12, 22", minute="0", day_of_week="*"), queue=getattr(settings, 'CELERY_PERIODIC_QUEUE','celery'))
def apps_update_calculated_properties():
    es = get_es()
    q = {"filter": {"and": [{"missing": {"field": "copy_of"}}]}}
    results = stream_es_query(q=q, es_url=ES_URLS["apps"], size=999999, chunksize=500)
    for r in results:
        calced_props = {"cp_is_active": is_app_active(r["_id"], r["_source"]["domain"])}
        es.post("%s/app/%s/_update" % (APP_INDEX, r["_id"]), data={"doc": calced_props})
