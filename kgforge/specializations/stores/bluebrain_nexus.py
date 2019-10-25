import asyncio
import json
import re
from urllib.parse import quote_plus
from collections import namedtuple
from enum import Enum
from typing import Optional, Union, Dict, List, Callable

import nest_asyncio
import nexussdk as nexus
from aiohttp import ClientSession

from kgforge.core import Resource, Resources
from kgforge.core.commons.actions import run, Actions, Action
from kgforge.core.commons.attributes import not_supported
from kgforge.core.commons.exceptions import catch
from kgforge.core.commons.typing import ManagedData, dispatch
from kgforge.core.commons.wrappers import DictWrapper
from kgforge.core.storing.exceptions import RegistrationError, DeprecationError, RetrievalError, TaggingError
from kgforge.core.storing.store import Store
from kgforge.core.transforming.converters import Converters


class BatchAction(Enum):
    CREATE = "create"
    FETCH = "fetch"
    DEPRECATE = "deprecate"
    UPDATE = "update"
    TAG = "tag"


BatchResult = namedtuple("BatchResult", ["resource", "response"])
BatchResults = List[BatchResult]


class BlueBrainNexus(Store):

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        try:
            self.organisation, self.project = self.bucket.split('/')
        except ValueError:
            raise ValueError("malformed bucket parameter, expecting 'organization/project' like")
        else:
            nexus.config.set_environment(self.endpoint)
            nexus.config.set_token(self.token)
            self.max_connections = 50
            self.headers = {
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/ld+json",
                "Accept": "application/ld+json"
            }
            self.url_requests = '/'.join((self.endpoint,
                                          'resources',
                                          quote_plus(self.organisation),
                                          quote_plus(self.project), '_'))
            # This is to make async working on the jupyter notebook
            nest_asyncio.apply()

    def _register_many(self, resources: Resources, update: bool) -> None:
        if update:
            succeeded, failures = self._batch(resources, action=BatchAction.UPDATE)
            action_name = f"{self._register_many.__name__}: {BatchAction.UPDATE}"
        else:
            succeeded, failures = self._batch(resources, action=BatchAction.CREATE)
            action_name = f"{self._register_many.__name__}: {BatchAction.CREATE}"
        actions = list()
        if succeeded:
            if update:
                actions.extend(self._synchronize_resources(succeeded, action_name, True, True))
            else:
                resources_without_context = list()
                for result in succeeded:
                    result.resource.id = result.response["@id"]
                    if not hasattr(result.resource, '_context'):
                        resources_without_context.append(result.resource)
                if resources_without_context:
                    ids_resources = {r.id: r for r in resources_without_context}
                    remote_succeed, remote_fail = self._batch(list(ids_resources.keys()), action=BatchAction.FETCH)
                    if remote_succeed:
                        for remote in remote_succeed:
                            ids_resources[remote.resource.id]._context = remote.resource._context
                            actions.append(self._synchronize_resource(ids_resources[remote.resource.id],
                                                                      remote.response, action_name, True, True))
                    if remote_fail:
                        for remote in remote_fail:
                            try:
                                identifier = remote.resource.id
                            except AttributeError:
                                identifier = remote.resource
                            actions.append(self._synchronize_resource(ids_resources[identifier], remote.response,
                                                                      action_name, True, False))
                else:
                    actions.extend(self._synchronize_resources(succeeded, action_name, True, True))
        if failures:
            actions.extend(self._synchronize_resources(results=failures, action_name=action_name,
                                                       succeeded=False, synchronized=False))
        print(Actions(actions))

    def _register_one(self, resource: Resource, update: bool) -> None:
        run(self._register, resource, status="_synchronized", update=update)

    def _register(self, resource: Resource, update: bool) -> None:
        if update and not hasattr(resource, "id"):
            raise RegistrationError(f"Can't update a resource that does not have an id")
        data = Converters.as_jsonld(resource, True, True)
        if update:
            if resource._synchronized:
                raise RegistrationError(f"Resource is synchronized, update has no effect")
            try:
                response = nexus.resources.update(data)
            except nexus.HTTPError as e:
                self._raise_nexus_http_error(e, RegistrationError)
            else:
                self._sync_metadata(resource, response)
        else:
            try:
                response = nexus.resources.create(org_label=self.organisation, project_label=self.project, data=data)
            except nexus.HTTPError as e:
                self._raise_nexus_http_error(e, RegistrationError)
            else:
                resource.id = response['@id']
                # If resource had no context, update it with the one provided by the store.
                if not hasattr(resource, '_context'):
                    remote = self.retrieve(resource.id)
                    resource._context = remote._context
                self._sync_metadata(resource, response)

    # C[R]UD

    @catch
    def retrieve(self, id: str, version: Optional[Union[int, str]] = None) -> Resource:
        try:
            if isinstance(version, int):
                response = nexus.resources.fetch(org_label=self.organisation, project_label=self.project,
                                                 resource_id=id, rev=version)
            else:
                response = nexus.resources.fetch(org_label=self.organisation, project_label=self.project,
                                                 resource_id=id, tag=version)
        except nexus.HTTPError as e:
            self._raise_nexus_http_error(e, RetrievalError)
        else:
            resource = self._to_resource(response)
            resource._synchronized = True
            self._sync_metadata(resource, response)
            return resource

    # CR[U]D

    @catch
    def tag(self, data: ManagedData, value: str) -> None:
        dispatch(data, self._tag_many, self._tag_one, value)

    def _tag_many(self, resources: Resources, tag: str) -> None:
        succeeded, failures = self._batch(resources, action=BatchAction.TAG, tag=tag)
        action_name = f"{self._tag_many.__name__}: {BatchAction.TAG}"
        actions = []
        if succeeded:
            actions.extend(self._synchronize_resources(succeeded, action_name, True, True))
        if failures:
            actions.extend(self._synchronize_resources(failures, action_name, False, False))
        print(Actions(actions))

    def _tag_one(self, resource: Resource, value: str) -> None:
        run(self._tag, resource, status="_synchronized", tag=value)

    def _tag(self, resource: Resource, value: str) -> None:
        if resource._synchronized is False:
            raise TaggingError("The resource has changes that have not being registered")
        if resource._last_action.operation == "_tag" and resource._last_action.succeeded:
            raise TaggingError(f"The current version of the resource has being already tagged")
        try:
            payload = Converters.as_jsonld(resource, True, True)
            response = nexus.resources.tag(payload, value)
            self._sync_metadata(resource, response)
        except nexus.HTTPError as e:
            self._raise_nexus_http_error(e, TaggingError)

    # CRU[D]

    @catch
    def deprecate(self, data: ManagedData) -> None:
        dispatch(data, self._deprecate_many, self._deprecate_one)

    def _deprecate_many(self, resources: Resources):
        succeeded, failures = self._batch(resources, action=BatchAction.DEPRECATE)
        action_name = f"{self._deprecate_many.__name__}: {BatchAction.DEPRECATE}"
        actions = list()
        if succeeded:
            actions.extend(self._synchronize_resources(succeeded, action_name, True, True))
        if failures:
            actions.extend(self._synchronize_resources(failures, action_name, False, False))
        print(Actions(actions))

    def _deprecate_one(self, resource: Resource):
        run(self._deprecate, resource, status="_synchronized")

    def _deprecate(self, resource: Resource):
        try:
            payload = Converters.as_jsonld(resource, True, True)
            response = nexus.resources.deprecate(payload)
            self._sync_metadata(resource, response)
        except nexus.HTTPError as e:
            self._raise_nexus_http_error(e, DeprecationError)

    # Query

    @catch
    def search(self, resolvers: "OntologiesHandler", *filters, **params) -> Resources:
        not_supported()

    @classmethod
    def _to_resource(cls, data: Dict) -> Resource:
        # FIXME: ideally a rdf-native solution

        def create_resource(dictionary: dict, ctx_base: str) -> Resource:
            rec = Resource()
            if "@id" in dictionary:
                rec.id = f"{ctx_base}{dictionary.pop('@id')}" if ctx_base else dictionary.pop("@id")
            if "@type" in dictionary:
                rec.type = dictionary.pop("@type")
            for k, v in dictionary.items():
                if not re.match(r"^_|@", k):
                    # TODO: use nexus namespace to properly identify metadata
                    if isinstance(v, dict):
                        setattr(rec, k, create_resource(v, ctx_base))
                    elif isinstance(v, list):
                        setattr(rec, k,
                                [create_resource(item, ctx_base) if isinstance(item, dict) else item for item in v])
                    else:
                        setattr(rec, k, v)
            return rec

        context = data.pop("@context", None)
        base = Converters.find_in_context(context, "@base") if context is not None else None
        resource = create_resource(data, base)
        if context is not None:
            resource._context = context
        return resource

    @staticmethod
    def _sync_metadata(resource: Resource, result: Dict) -> None:
        # TODO: use nexus namespace to properly identify its metadata
        metadata = {k: v for k, v in result.items() if k.startswith("_")}
        resource._store_metadata = DictWrapper._wrap(metadata)

    def _synchronize_resources(self, results: BatchResults, action_name: str,
                               succeeded: bool, synchronized: bool) -> Actions:
        actions = list()
        for result in results:
            action = self._synchronize_resource(result.resource, result.response, action_name, succeeded, synchronized)
            actions.append(action)
        return Actions(actions)

    def _synchronize_resource(self, resource: Resource, response: dict, action_name: str,
                              succeeded: bool, synchronized: bool) -> Action:
        error = None if succeeded else f"{response['@type']}: {response['reason']}"
        action = Action(action_name, succeeded, error)
        resource._last_action = action
        resource._synchronized = synchronized
        if succeeded:
            self._sync_metadata(resource, response)
        return action

    def _batch(self, resources: Union[Resources, List], action: BatchAction, **kwargs) -> (BatchResults, BatchResults):
        loop = asyncio.get_event_loop()
        failure = list()
        success = list()

        async def dispatch_action():
            futures = []
            semaphore = asyncio.Semaphore(self.max_connections)
            async with ClientSession() as session:
                for resource in resources:
                    if action in [action.UPDATE, action.TAG, action.DEPRECATE]:
                        try:
                            rid = resource.id
                        except AttributeError:
                            response = {"@type": "BadPayload", "reason": "can't update resources that doesn't have id"}
                            failure.append(BatchResult(resource, response))
                            continue
                    if action == action.CREATE:
                        payload = Converters.as_jsonld(resource)
                        request = asyncio.ensure_future(
                            queue_post(semaphore=semaphore, session=session,
                                       url=self.url_requests, resource=resource, payload=payload))
                    if action == action.UPDATE:
                        url = "/".join((self.url_requests, quote_plus(rid)))
                        if resource._synchronized:
                            response = {"@type": "Unchanged", "reason": "Resource unchanged, update didn't happened"}
                            failure.append(BatchResult(resource, response))
                            continue
                        params = {"rev": resource._store_metadata["_rev"]}
                        payload = Converters.as_jsonld(resource)
                        request = asyncio.ensure_future(
                            queue_put(semaphore=semaphore, session=session,
                                      url=url, resource=resource, payload=payload, params=params))
                    if action == action.TAG:
                        url = "/".join((self.url_requests, quote_plus(rid), "tags"))
                        rev = resource._store_metadata["_rev"]
                        params = {"rev": rev}
                        payload = {"tag": kwargs.get("tag"), "rev": rev}
                        request = asyncio.ensure_future(queue_post(semaphore=semaphore, session=session,
                                                                   url=url, resource=resource,
                                                                   payload=payload, params=params))
                    if action == action.DEPRECATE:
                        url = "/".join((self.url_requests, quote_plus(rid)))
                        params = {"rev": resource._store_metadata["_rev"]}
                        request = asyncio.ensure_future(queue_delete(semaphore=semaphore, session=session,
                                                                     url=url, resource=resource, params=params))
                    futures.append(request)
                await asyncio.gather(*futures)

        async def fetch():
            futures = list()
            semaphore = asyncio.Semaphore(self.max_connections)
            async with ClientSession() as session:
                for resource in resources:
                    try:
                        identifier = resource.id
                    except AttributeError:
                        identifier = resource
                    url = "/".join((self.url_requests, quote_plus(identifier)))
                    request = asyncio.ensure_future(queue_get(semaphore, session, url, resource))
                    futures.append(request)
                await asyncio.gather(*futures)

        async def queue_post(semaphore, session, url, resource, payload, params=None):
            async with semaphore:
                await post(session=session, url=url, resource=resource, payload=payload, params=params)

        async def queue_put(semaphore, session, url, resource, payload, params=None):
            async with semaphore:
                await put(session=session, url=url, resource=resource, payload=payload, params=params)

        async def queue_get(semaphore, session, url, resource):
            async with semaphore:
                await get(session, url, resource)

        async def queue_delete(semaphore, session, url, resource, params):
            async with semaphore:
                await delete(session=session, url=url, resource=resource, params=params)

        async def post(session, url, resource, payload, params):
            async with session.post(url, headers=self.headers, data=json.dumps(payload), params=params) as response:
                content = await response.json()
                if response.status == 201:
                    success.append(BatchResult(resource, content))
                else:
                    failure.append(BatchResult(resource, content))

        async def put(session, url, resource, payload, params):
            async with session.put(url, headers=self.headers, data=json.dumps(payload), params=params) as response:
                content = await response.json()
                if response.status == 200:
                    success.append(BatchResult(resource, content))
                else:
                    failure.append(BatchResult(resource, content))

        async def get(session, url, resource):
            async with session.get(url, headers=self.headers) as response:
                response_content = await response.json()
                if response.status == 200:
                    resource = self._to_resource(response_content)
                    success.append(BatchResult(resource, response_content))
                else:
                    failure.append(BatchResult(resource, response_content))

        async def delete(session, url, resource, params):
            async with session.delete(url, headers=self.headers, params=params) as response:
                response_content = await response.json()
                if response.status == 200:
                    success.append(BatchResult(resource, response_content))
                else:
                    failure.append(BatchResult(resource, response_content))

        action_func = {
            BatchAction.CREATE: dispatch_action,
            BatchAction.UPDATE: dispatch_action,
            BatchAction.FETCH: fetch,
            BatchAction.TAG: dispatch_action,
            BatchAction.DEPRECATE: dispatch_action
        }
        loop.run_until_complete(action_func.get(action, not_supported)())
        return success, failure

    @staticmethod
    def _raise_nexus_http_error(error: nexus.HTTPError, error_type: Callable):
        try:
            reason = error.response.json()["reason"]
        except IndexError:
            reason = error.response.text()
        finally:
            raise error_type(reason)
