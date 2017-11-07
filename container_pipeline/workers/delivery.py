#!/usr/bin/env python

import json
import logging
import os
import time

from container_pipeline.lib.log import load_logger
from container_pipeline.lib.openshift import Openshift, OpenshiftError
from container_pipeline.utils import Build
from container_pipeline.workers.base import BaseWorker


class DeliveryWorker(BaseWorker):
    """
    Delivery Worker tags the image built by Build Worker using the
    `desired-tag` field in index entry
    """
    NAME = 'Delivery worker'

    def __init__(self, logger=None, sub=None, pub=None):
        super(DeliveryWorker, self).__init__(logger, sub, pub)
        self.openshift = Openshift(logger=self.logger)

    def handle_job(self, job):
        """Handles a job meant for delivery worker"""
        # TODO: this needs to be addressed after addressing CentOS#278
        self.job = job
        self.logger.info('Starting delivery for job: {}'.format(self.job))

        success = self.deliver_build()

        if success:
            self.handle_delivery_success()
        else:
            self.handle_delivery_failure()

    def deliver_build(self):
        """
        Runs an `oc build` with the `run_delivery.sh` script as a part of build
        template. It mainly changes the tag of the image from a test tag
        generated by build process to the tag desired by user as mentioned in
        `desired-tag` field in cccp.yml
        """
        project_hash_key = self.job["project_hash_key"]

        try:
            self.openshift.login()
            # start the 'delivery' build
            delivery_id = self.openshift.build(project_hash_key, 'delivery')
        except OpenshiftError as e:
            self.logger.error(e)
            return False
        else:
            if not delivery_id:
                return False

        delivery_status = self.openshift.wait_for_build_status(
            project_hash_key, delivery_id, 'Complete', status_index=2)
        logs = self.openshift.get_build_logs(
                project_hash_key, delivery_id, "delivery")
        delivery_logs_file = os.path.join(
            self.job['logs_dir'], 'delivery_logs.txt')
        self.export_logs(logs, delivery_logs_file)
        return delivery_status

    def handle_delivery_success(self):
        """
        - Marks project build as complete
        - Sends job details to RPM tracking piece and deletes the job from the
        tube
        """
        # Mark project build as complete
        Build(self.job['namespace'], logger=self.logger).complete()
        self.logger.debug('Marked project build: {} as complete.'.format(
            self.job['namespace']))
        self.logger.debug('Putting job details to master_tube for tracker\'s'
                          ' consumption')

        # sending notification as delivery complete and also addingn this into
        # tracker.
        self.job['action'] = 'notify_user'
        self.queue.put(json.dumps(self.job), 'master_tube')

        # Put some delay to avoid mismatch in uploading jod details to
        # master_tube
        time.sleep(10)
        self.job['action'] = 'tracking'
        self.queue.put(json.dumps(self.job), 'master_tube')

    def handle_delivery_failure(self):
        """
        Puts the job back to the delivery tube for later attempt at delivery
        and requests to notify the user about failure to deliver
        """
        self.job["build_status"] = False
        self.job['action'] = "notify_user"
        self.queue.put(json.dumps(self.job), 'master_tube')
        self.logger.warning(
            "Delivery is not successful. Notifying the user.")
        # data = {
        #     'action': 'notify_user',
        #     'namespace': self.job["namespace"],
        #     'build_status': False,
        #     'notify_email': self.job['notify_email'],
        #     'delivery_logs_file': os.path.join(
        #         self.job['logs_dir'], 'delivery_logs.txt'),
        #     'logs_dir': self.job['logs_dir'],
        #     'project_name': self.job["project_name"],
        #     'job_name': self.job['jobid'],
        #     'test_tag': self.job['test_tag']}
        # self.notify(data)


if __name__ == "__main__":
    load_logger()
    logger = logging.getLogger('delivery-worker')
    worker = DeliveryWorker(logger, sub='start_delivery',
                            pub='delivery_failed')
    worker.run()
