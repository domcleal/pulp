# -*- coding: utf-8 -*-
#
# Copyright © 2011 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

# Python
import logging

# 3rd Party
import web
from web.webapi import BadRequest

# Pulp
import pulp.server.managers.factory as managers
from pulp.common.tags import action_tag, resource_tag
from pulp.server import config as pulp_config
from pulp.server.auth.authorization import READ, CREATE, UPDATE, DELETE
from pulp.server.db.model.criteria import Criteria
from pulp.server.dispatch import constants as dispatch_constants
from pulp.server.dispatch import factory as dispatch_factory
from pulp.server.dispatch.call import CallRequest, CallReport
from pulp.server.itineraries.consumer import (
    consumer_content_install_itinerary, consumer_content_uninstall_itinerary,
    consumer_content_update_itinerary)
from pulp.server.exceptions import MissingResource, MissingValue
from pulp.server.itineraries.bind import (
    bind_itinerary, unbind_itinerary, forced_unbind_itinerary)
from pulp.server.webservices.controllers.search import SearchController
from pulp.server.webservices.controllers.base import JSONController
from pulp.server.webservices.controllers.decorators import auth_required
from pulp.server.webservices import execution
from pulp.server.webservices import serialization

# -- constants ----------------------------------------------------------------

_LOG = logging.getLogger(__name__)


def expand_consumers(options, consumers):
    """
    Expand a list of users based on flags specified in the
    post body or query parameters.  The _href is always added by the
    serialization function used.
    Supported options:
      details - synonym for: (bindings=True,)
      bindings - include bindings
    @param options: The (expanding) options.
    @type options: dict
    @param consumers: A list of consumers
    @type consumers: list
    @return: A list of expanded consumers.
    @rtype: list
    """
    if options.get('details', False):
        options['bindings'] = True
    # add bindings
    if options.get('bindings', False):
        ids = [c['id'] for c in consumers]
        manager = managers.consumer_bind_manager()
        criteria = Criteria({'consumer_id':{'$in':ids}})
        bindings = manager.find_by_criteria(criteria)
        collated = {}
        for b in bindings:
            lst = collated.setdefault(b['consumer_id'], [])
            lst.append(b)
        for consumer in consumers:
            consumer['bindings'] = \
                [serialization.binding.serialize(b, False)
                    for b in collated.get(consumer['id'], [])]
    return consumers


# -- controllers --------------------------------------------------------------

class Consumers(JSONController):

    @auth_required(READ)
    def GET(self):
        params = web.input()
        manager = managers.consumer_query_manager()
        consumers = expand_consumers(params, manager.find_all())
        for c in consumers:
            href = serialization.link.child_link_obj(c['id'])
            c.update(href)
        return self.ok(consumers)

    @auth_required(CREATE)
    def POST(self):
        body = self.params()
        id = body.get('id')
        display_name = body.get('display_name')
        description = body.get('description')
        notes = body.get('notes')

        manager = managers.consumer_manager()
        args = [id, display_name, description, notes]
        weight = pulp_config.config.getint('tasks', 'create_weight')
        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, id),
                action_tag('create')]

        call_request = CallRequest(manager.register,
                                   args,
                                   weight=weight,
                                   tags=tags)
        call_request.creates_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, id)

        call_report = CallReport.from_call_request(call_request)
        call_report.serialize_result = False

        consumer = execution.execute_sync(call_request, call_report)
        consumer.update({'_href': serialization.link.child_link_obj(consumer['id'])})
        return self.created(consumer['_href'], consumer)

class Consumer(JSONController):

    @auth_required(READ)
    def GET(self, id):
        params = web.input()
        manager = managers.consumer_manager()
        consumer = manager.get_consumer(id)
        consumer = expand_consumers(params, [consumer])[0]
        href = serialization.link.current_link_obj()
        consumer.update(href)
        return self.ok(consumer)

    @auth_required(DELETE)
    def DELETE(self, id):
        manager = managers.consumer_manager()
        resources = {
            dispatch_constants.RESOURCE_CONSUMER_TYPE:
                {id: dispatch_constants.RESOURCE_DELETE_OPERATION}
        }
        tags = [
            resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, id),
            action_tag('delete'),
        ]
        call_request = CallRequest(
            manager.unregister,
            [id],
            resources=resources,
            tags=tags)
        return self.ok(execution.execute(call_request))

    @auth_required(UPDATE)
    def PUT(self, id):
        body = self.params()
        delta = body.get('delta')
        manager = managers.consumer_manager()
        resources = {
            dispatch_constants.RESOURCE_CONSUMER_TYPE:
                {id: dispatch_constants.RESOURCE_UPDATE_OPERATION}
        }
        tags = [
            resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, id),
            action_tag('update')
        ]
        call_request = CallRequest(
            manager.update,
            [id, delta],
            resources=resources,
            tags=tags)
        consumer = execution.execute(call_request)
        href = serialization.link.current_link_obj()
        consumer.update(href)
        return self.ok(consumer)


class ConsumerSearch(SearchController):

    def __init__(self):
        super(ConsumerSearch, self).__init__(
            managers.consumer_query_manager().find_by_criteria)

    def GET(self):
        params = web.input()
        ignored = ('details', 'bindings')
        found = self._get_query_results_from_get(ignored)
        consumers = expand_consumers(params, found)
        for c in consumers:
            href = serialization.link.search_safe_link_obj(c['id'])
            c.update(href)
        return self.ok(consumers)

    def POST(self):
        body = self.params()
        found = self._get_query_results_from_post()
        consumers = expand_consumers(body, found)
        for c in consumers:
            href = serialization.link.search_safe_link_obj(c['id'])
            c.update(href)
        return self.ok(consumers)



class Bindings(JSONController):
    """
    Consumer I{bindings} represents the collection of
    objects used to associate a consumer and a repo-distributor
    association.  Users wanting to create this association will
    create an object in this collection.  Both bind and unbind
    is idempotent.
    """

    #@auth_required(READ)
    def GET(self, consumer_id, repo_id=None):
        """
        Fetch all bind objects referencing the
        specified I{consumer_id}.
        @param consumer_id: The specified consumer.
        @type consumer_id: str
        @return: A list of bind dict:
            {consumer_id:<str>,
             repo_id:<str>,
             distributor_id:<str>,
             href:<str>,
             type_id:<str>,
             details:<dict>}
        @rtype: dict
        """
        manager = managers.consumer_bind_manager()
        bindings = manager.find_by_consumer(consumer_id, repo_id)
        bindings = [serialization.binding.serialize(b) for b in bindings]
        return self.ok(bindings)

    @auth_required(CREATE)
    def POST(self, consumer_id):
        """
        Create a bind association between the specified
        consumer by id included in the URL path and a repo-distributor
        specified in the POST body: {repo_id:<str>, distributor_id:<str>}.
        Designed to be idempotent so only MissingResource is expected to
        be raised by manager.
        @param consumer_id: The consumer to bind.
        @type consumer_id: str
        @return: The list of call_reports
        @rtype: list
        """
        # validate consumer
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        # get other options and validate them
        body = self.params()
        repo_id = body.get('repo_id')
        distributor_id = body.get('distributor_id')
        options = body.get('options', {})

        managers.repo_query_manager().get_repository(repo_id)
        managers.repo_distributor_manager().get_distributor(repo_id, distributor_id)

        # bind
        call_requests = bind_itinerary(consumer_id, repo_id, distributor_id, options)
        execution.execute_multiple(call_requests)


class Binding(JSONController):
    """
    Represents a specific bind resource.
    """

    @auth_required(READ)
    def GET(self, consumer_id, repo_id, distributor_id):
        """
        Fetch a specific bind object which represents a specific association
        between a consumer and repo-distributor.
        @param consumer_id: A consumer ID.
        @type consumer_id: str
        @param repo_id: A repo ID.
        @type repo_id: str
        @param distributor_id: A distributor ID.
        @type distributor_id: str
        @return: A specific bind object:
            {consumer_id:<str>,
             repo_id:<str>,
             distributor_id:<str>,
             href:<str>,
             type_id:<str>,
             details:<dict>}
        @rtype: dict
        """
        manager = managers.consumer_bind_manager()
        bind = manager.get_bind(consumer_id, repo_id, distributor_id)
        serialized_bind = serialization.binding.serialize(bind)
        return self.ok(serialized_bind)

    @auth_required(DELETE)
    def DELETE(self, consumer_id, repo_id, distributor_id):
        """
        Delete a bind association between the specified
        consumer and repo-distributor.  Designed to be idempotent.
        @param consumer_id: A consumer ID.
        @type consumer_id: str
        @param repo_id: A repo ID.
        @type repo_id: str
        @param distributor_id: A distributor ID.
        @type distributor_id: str
        @return: The list of call_reports
        @rtype: list
        """
        body = self.params()
        # validate resources
        manager = managers.consumer_bind_manager()
        manager.get_bind(consumer_id, repo_id, distributor_id)
        # delete (unbind)
        forced = body.get('force', False)
        options = body.get('options', {})
        if forced:
            call_requests = forced_unbind_itinerary(
                consumer_id,
                repo_id,
                distributor_id,
                options)
        else:
            call_requests = unbind_itinerary(
                consumer_id,
                repo_id,
                distributor_id,
                options)
        execution.execute_multiple(call_requests)


class BindingSearch(SearchController):
    """
    Bind search.
    """
    def __init__(self):
        SearchController.__init__(self, managers.consumer_bind_manager().find_by_criteria)


class Content(JSONController):
    """
    Represents a specific bind object.
    """

    @auth_required(CREATE)
    def POST(self, id, action):
        """
        Content actions.
        """
        method = getattr(self, action, None)
        if method:
            return method(id)
        else:
            raise BadRequest()

    def install(self, id):
        """
        Install content (units) on a consumer.
        Expected body: {units:[], options:<dict>}
        where unit is: {type_id:<str>, unit_key={}} and the
        options is a dict of install options.
        @param id: A consumer ID.
        @type id: str
        @return: TBD
        @rtype: dict
        """
        body = self.params()
        units = body.get('units')
        options = body.get('options')
        call_request = consumer_content_install_itinerary(id, units, options)[0]
        result = execution.execute_async(self, call_request)
        return result

    def update(self, id):
        """
        Update content (units) on a consumer.
        Expected body: {units:[], options:<dict>}
        where unit is: {type_id:<str>, unit_key={}} and the
        options is a dict of update options.
        @param id: A consumer ID.
        @type id: str
        @return: TBD
        @rtype: dict
        """
        body = self.params()
        units = body.get('units')
        options = body.get('options')
        call_request = consumer_content_update_itinerary(id, units, options)[0]
        result = execution.execute_async(self, call_request)
        return result

    def uninstall(self, id):
        """
        Uninstall content (units) on a consumer.
        Expected body: {units:[], options:<dict>}
        where unit is: {type_id:<str>, unit_key={}} and the
        options is a dict of uninstall options.
        @param id: A consumer ID.
        @type id: str
        @return: TBD
        @rtype: dict
        """
        body = self.params()
        units = body.get('units')
        options = body.get('options')
        call_request = consumer_content_uninstall_itinerary(id, units, options)[0]
        result = execution.execute_async(self, call_request)
        return result

class ConsumerHistory(JSONController):

    @auth_required(READ)
    def GET(self, id):
        """
        @type id: str
        @param id: consumer id
        """
        valid_filters = ['event_type', 'limit', 'sort', 'start_date', 'end_date']
        filters = self.filters(valid_filters)
        event_type = filters.get('event_type', None)
        limit = filters.get('limit', None)
        sort = filters.get('sort', None)
        start_date = filters.get('start_date', None)
        end_date = filters.get('end_date', None)

        if sort is None:
            sort = 'descending'
        else:
            sort = sort[0]

        if limit:
            limit = int(limit[0])

        if start_date:
            start_date = start_date[0]

        if end_date:
            end_date = end_date[0]

        if event_type:
            event_type = event_type[0]

        results = managers.consumer_history_manager().query(consumer_id=id, event_type=event_type, limit=limit,
                                    sort=sort, start_date=start_date, end_date=end_date)
        return self.ok(results)


class Profiles(JSONController):
    """
    Consumer I{profiles} represents the collection of
    objects used to associate consumers and installed content
    unit profiles.
    """

    @auth_required(READ)
    def GET(self, consumer_id):
        """
        Get all profiles associated with a consumer.
        @param consumer_id: The consumer ID.
        @type consumer_id: str
        @return: A list of profiles:
          profile is: {consumer_id:<str>, content_type:<str>, profile:<dict>}
        @return: list
        """
        manager = managers.consumer_profile_manager()
        profiles = manager.get_profiles(consumer_id)
        profiles = [serialization.consumer.profile(p) for p in profiles]
        return self.ok(profiles)

    @auth_required(CREATE)
    def POST(self, consumer_id):
        """
        Associate a profile with a consumer by content type ID.
        @param consumer_id: A consumer ID.
        @type consumer_id: str
        @return: The created model object:
            {consumer_id:<str>, content_type:<str>, profile:<dict>}
        @rtype: dict
        """
        body = self.params()
        content_type = body.get('content_type')
        profile = body.get('profile')

        manager = managers.consumer_profile_manager()
        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_CONTENT_UNIT_TYPE, content_type),
                action_tag('profile_create')]

        call_request = CallRequest(manager.create,
                                   [consumer_id, content_type],
                                   {'profile': profile},
                                   tags=tags,
                                   weight=0,
                                   kwarg_blacklist=['profile'])
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)

        call_report = CallReport.from_call_request(call_request)
        call_report.serialize_result = False

        consumer = execution.execute_sync(call_request, call_report)
        link = serialization.link.child_link_obj(consumer_id, content_type)
        consumer.update(link)

        return self.created(link['_href'], consumer)


class Profile(JSONController):
    """
    Consumer I{profiles} represents the collection of
    objects used to associate consumers and installed content
    unit profiles.
    """

    @auth_required(READ)
    def GET(self, consumer_id, content_type):
        """
        @param consumer_id: The consumer ID.
        @type consumer_id: str
        """
        manager = managers.consumer_profile_manager()
        profile = manager.get_profile(consumer_id, content_type)
        serialized = serialization.consumer.profile(profile)
        return self.ok(serialized)

    @auth_required(UPDATE)
    def PUT(self, consumer_id, content_type):
        """
        Update the association of a profile with a consumer by content type ID.
        @param consumer_id: A consumer ID.
        @type consumer_id: str
        @param content_type: A content unit type ID.
        @type content_type: str
        @return: The updated model object:
            {consumer_id:<str>, content_type:<str>, profile:<dict>}
        @rtype: dict
        """
        body = self.params()
        profile = body.get('profile')

        manager = managers.consumer_profile_manager()
        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_CONTENT_UNIT_TYPE, content_type),
                action_tag('profile_update')]

        call_request = CallRequest(manager.update,
                                   [consumer_id, content_type],
                                   {'profile': profile},
                                   tags=tags,
                                   weight=0,
                                   kwarg_blacklist=['profile'])
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)

        call_report = CallReport.from_call_request(call_request)
        call_report.serialize_result = False

        consumer = execution.execute_sync(call_request, call_report)
        link = serialization.link.child_link_obj(consumer_id, content_type)
        consumer.update(link)

        return self.ok(consumer)

    @auth_required(DELETE)
    def DELETE(self, consumer_id, content_type):
        """
        Delete an association between the specified
        consumer and profile.  Designed to be idempotent.
        @param consumer_id: A consumer ID.
        @type consumer_id: str
        @param content_type: The content type ID.
        @type content_type: str
        @return: The deleted model object:
            {consumer_id:<str>, content_type:<str>, profile:<dict>}
            Or, None if bind does not exist.
        @rtype: dict
        """
        manager = managers.consumer_profile_manager()
        resources = {
            dispatch_constants.RESOURCE_CONSUMER_TYPE:
                {consumer_id:dispatch_constants.RESOURCE_READ_OPERATION},
        }
        args = [
            consumer_id,
            content_type,
        ]
        tags = [
            resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
        ]
        call_request = CallRequest(manager.delete,
                                   args=args,
                                   resources=resources,
                                   tags=tags)
        return self.ok(execution.execute(call_request))


class ContentApplicability(JSONController):
    """
    Determine content applicability.
    """

    @auth_required(READ)
    def POST(self):
        """
        Determine content applicability.
        body {
        consumer_criteria:<dict> or None, 
        repo_criteria:<dict> or None, 
        units: {<type_id1> : [{<unit1>}, {<unit2}, ..]
                <type_id2> : [{<unit1>}, {<unit2}, ..]} or None
        }

        :return: A dict of applicability reports keyed by consumer ID.
            Each consumer report is:
                { <unit_type_id1> : [<ApplicabilityReport>],
                  <unit_type_id1> : [<ApplicabilityReport>]},
                }
                
        :rtype: dict
        """
        body = self.params()

        consumer_criteria = body.get('consumer_criteria', None)
        repo_criteria = body.get('repo_criteria', None)
        units = body.get('units', None)

        if consumer_criteria:
            consumer_criteria = Criteria.from_client_input(consumer_criteria)

        if repo_criteria:
            repo_criteria = Criteria.from_client_input(repo_criteria)

        manager = managers.consumer_applicability_manager()
        report = manager.units_applicable(consumer_criteria, repo_criteria, units)

        for consumer_report in report.values():
            for unit_type_id, report_list in consumer_report.items():
                consumer_report[unit_type_id] = [serialization.consumer.applicability_report(r) for r in report_list]
        return self.ok(report)


class UnitInstallScheduleCollection(JSONController):

    @auth_required(READ)
    def GET(self, consumer_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        consumer_tag = resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        install_tag = action_tag('scheduled_unit_install')

        scheduler = dispatch_factory.scheduler()
        scheduled_calls = scheduler.find(consumer_tag, install_tag)

        schedule_objs = []
        for call in scheduled_calls:
            obj = serialization.dispatch.scheduled_unit_management_obj(call)
            obj.update(serialization.link.child_link_obj(obj['_id']))
            schedule_objs.append(obj)
        return self.ok(schedule_objs)

    @auth_required(CREATE)
    def POST(self, consumer_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_data = self.params()
        units = schedule_data.pop('units', None)
        install_options = {'options': schedule_data.pop('options', {})}

        if not units:
            raise MissingValue(['units'])

        schedule_manager = managers.schedule_manager()

        weight = pulp_config.config.getint('tasks', 'create_weight')
        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                action_tag('create_unit_install_schedule')]

        call_request = CallRequest(schedule_manager.create_unit_install_schedule,
                                   [consumer_id, units, install_options, schedule_data],
                                   weight=weight,
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)

        schedule_id = execution.execute_sync(call_request)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.child_link_obj(schedule_id))
        return self.created(scheduled_obj['_href'], scheduled_obj)


class UnitInstallScheduleResource(JSONController):

    @auth_required(READ)
    def GET(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        if consumer_id not in scheduled_call['call_request'].args:
            raise MissingResource(consumer=consumer_id, unit_install_schedule=schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.current_link_obj())
        return self.ok(scheduled_obj)

    @auth_required(UPDATE)
    def PUT(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_data = self.params()
        install_options = None
        units = schedule_data.pop('units', None)

        if 'options' in schedule_data:
            install_options = {'options': schedule_data.pop('options')}

        schedule_manager = managers.schedule_manager()

        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id),
                action_tag('update_unit_install_schedule')]

        call_request = CallRequest(schedule_manager.update_unit_install_schedule,
                                   [consumer_id, schedule_id, units, install_options, schedule_data],
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        call_request.updates_resource(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id)

        execution.execute(call_request)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.current_link_obj())
        return self.ok(scheduled_obj)

    @auth_required(DELETE)
    def DELETE(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_manager = managers.schedule_manager()

        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id),
                action_tag('delete_unit_install_schedule')]

        call_request = CallRequest(schedule_manager.delete_unit_install_schedule,
                                   [consumer_id, schedule_id],
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        call_request.deletes_resource(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id)

        result = execution.execute(call_request)
        return self.ok(result)


class UnitUpdateScheduleCollection(JSONController):

    @auth_required(READ)
    def GET(self, consumer_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        consumer_tag = resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        update_tag = action_tag('scheduled_unit_update')

        scheduler = dispatch_factory.scheduler()
        scheduled_calls = scheduler.find(consumer_tag, update_tag)

        schedule_objs = []
        for call in scheduled_calls:
            obj = serialization.dispatch.scheduled_unit_management_obj(call)
            obj.update(serialization.link.child_link_obj(obj['_id']))
            schedule_objs.append(obj)
        return self.ok(schedule_objs)

    @auth_required(CREATE)
    def POST(self, consumer_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_data = self.params()
        units = schedule_data.pop('units', None)
        update_options = {'options': schedule_data.pop('options', {})}

        if not units:
            raise MissingValue(['units'])

        schedule_manager = managers.schedule_manager()

        weight = pulp_config.config.getint('tasks', 'create_weight')
        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                action_tag('create_unit_update_schedule')]

        call_request = CallRequest(schedule_manager.create_unit_update_schedule,
                                   [consumer_id, units, update_options, schedule_data],
                                   weight=weight,
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)

        schedule_id = execution.execute_sync(call_request)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.child_link_obj(schedule_id))
        return self.created(scheduled_obj['_href'], scheduled_obj)


class UnitUpdateScheduleResource(JSONController):

    @auth_required(READ)
    def GET(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        if consumer_id not in scheduled_call['call_request'].args:
            raise MissingResource(consumer=consumer_id, unit_update_schedule=schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.current_link_obj())
        return self.ok(scheduled_obj)

    @auth_required(UPDATE)
    def PUT(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_data = self.params()
        install_options = None
        units = schedule_data.pop('units', None)

        if 'options' in schedule_data:
            install_options = {'options': schedule_data.pop('options')}

        schedule_manager = managers.schedule_manager()

        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id),
                action_tag('update_unit_update_schedule')]

        call_request = CallRequest(schedule_manager.update_unit_update_schedule,
                                   [consumer_id, schedule_id, units, install_options, schedule_data],
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        call_request.updates_resource(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id)

        execution.execute(call_request)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.current_link_obj())
        return self.ok(scheduled_obj)

    @auth_required(DELETE)
    def DELETE(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_manager = managers.schedule_manager()

        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id),
                action_tag('delete_unit_update_schedule')]

        call_request = CallRequest(schedule_manager.delete_unit_update_schedule,
                                   [consumer_id, schedule_id],
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        call_request.deletes_resource(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id)
        result = execution.execute(call_request)
        return self.ok(result)


class UnitUninstallScheduleCollection(JSONController):

    @auth_required(READ)
    def GET(self, consumer_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        consumer_tag = resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        uninstall_tag = action_tag('scheduled_unit_uninstall')

        scheduler = dispatch_factory.scheduler()
        scheduled_calls = scheduler.find(consumer_tag, uninstall_tag)

        schedule_objs = []
        for call in scheduled_calls:
            obj = serialization.dispatch.scheduled_unit_management_obj(call)
            obj.update(serialization.link.child_link_obj(obj['_id']))
            schedule_objs.append(obj)
        return self.ok(schedule_objs)

    @auth_required(CREATE)
    def POST(self, consumer_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_data = self.params()
        units = schedule_data.pop('units', None)
        uninstall_options = {'options': schedule_data.pop('options', {})}

        if not units:
            raise MissingValue(['units'])

        schedule_manager = managers.schedule_manager()

        weight = pulp_config.config.getint('tasks', 'create_weight')
        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                action_tag('create_unit_uninstall_schedule')]

        call_request = CallRequest(schedule_manager.create_unit_uninstall_schedule,
                                   [consumer_id, units, uninstall_options, schedule_data],
                                   weight=weight,
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)

        schedule_id = execution.execute_sync(call_request)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.child_link_obj(schedule_id))
        return self.created(scheduled_obj['_href'], scheduled_obj)


class UnitUninstallScheduleResource(JSONController):

    @auth_required(READ)
    def GET(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        if consumer_id not in scheduled_call['call_request'].args:
            raise MissingResource(consumer=consumer_id, unit_uninstall_schedule=schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.current_link_obj())
        return self.ok(scheduled_obj)

    @auth_required(UPDATE)
    def PUT(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_data = self.params()
        install_options = None
        units = schedule_data.pop('units', None)

        if 'options' in schedule_data:
            install_options = {'options': schedule_data.pop('options')}

        schedule_manager = managers.schedule_manager()

        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id),
                action_tag('update_unit_uninstall_schedule')]

        call_request = CallRequest(schedule_manager.update_unit_uninstall_schedule,
                                   [consumer_id, schedule_id, units, install_options, schedule_data],
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        call_request.updates_resource(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id)

        execution.execute(call_request)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.current_link_obj())
        return self.ok(scheduled_obj)

    @auth_required(DELETE)
    def DELETE(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_manager = managers.schedule_manager()

        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id),
                action_tag('delete_unit_uninstall_schedule')]

        call_request = CallRequest(schedule_manager.delete_unit_uninstall_schedule,
                                   [consumer_id, schedule_id],
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        call_request.deletes_resource(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id)

        result = execution.execute(call_request)
        return self.ok(result)

# -- web.py application -------------------------------------------------------

urls = (
    '/$', Consumers,
    '/search/$', ConsumerSearch,
    '/binding/search/$', BindingSearch,
    '/actions/content/applicability/$', ContentApplicability,
    '/([^/]+)/bindings/$', Bindings,
    '/([^/]+)/bindings/([^/]+)/$', Bindings,
    '/([^/]+)/bindings/([^/]+)/([^/]+)/$', Binding,
    '/([^/]+)/profiles/$', Profiles,
    '/([^/]+)/profiles/([^/]+)/$', Profile,
    '/([^/]+)/schedules/content/install/', UnitInstallScheduleCollection,
    '/([^/]+)/schedules/content/install/([^/]+)/', UnitInstallScheduleResource,
    '/([^/]+)/schedules/content/update/', UnitUpdateScheduleCollection,
    '/([^/]+)/schedules/content/update/([^/]+)/', UnitUpdateScheduleResource,
    '/([^/]+)/schedules/content/uninstall/', UnitUninstallScheduleCollection,
    '/([^/]+)/schedules/content/uninstall/([^/]+)/', UnitUninstallScheduleResource,
    '/([^/]+)/actions/content/(install|update|uninstall)/$', Content,
    '/([^/]+)/history/$', ConsumerHistory,
    '/([^/]+)/$', Consumer,
)

application = web.application(urls, globals())
