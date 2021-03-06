import logging
from urlparse import urlparse
import ckanapi
import os
import routes

from pylons import config

import ckan.plugins.toolkit as toolkit
from ckan.lib.celery_app import celery
from ckan.lib.helpers import get_pkg_dict_extra
from ckanext.syndicate.plugin import (
    get_syndicate_flag,
    get_syndicated_id,
    get_syndicated_name_prefix,
    get_syndicated_organization,
    get_syndicated_author,
    is_organization_preserved,
)

logger = logging.getLogger(__name__)


@celery.task(name='syndicate.sync_package')
def sync_package_task(package, action, ckan_ini_filepath):
    logger = sync_package_task.get_logger()
    load_config(ckan_ini_filepath)
    register_translator()
    logger.info("Sync package %s, with action %s" % (package, action))
    return sync_package(package, action)


# TODO: why mp this
# enable celery logging for when you run nosetests -s
log = logging.getLogger('ckanext.syndicate.tasks')


def get_logger():
    return log
sync_package_task.get_logger = get_logger


def load_config(ckan_ini_filepath):
    import paste.deploy
    config_abs_path = os.path.abspath(ckan_ini_filepath)
    conf = paste.deploy.appconfig('config:' + config_abs_path)
    import ckan
    ckan.config.environment.load_environment(conf.global_conf,
                                             conf.local_conf)

    ## give routes enough information to run url_for
    parsed = urlparse(conf.get('ckan.site_url', 'http://0.0.0.0'))
    request_config = routes.request_config()
    request_config.host = parsed.netloc + parsed.path
    request_config.protocol = parsed.scheme


def register_translator():
    # https://github.com/ckan/ckanext-archiver/blob/master/ckanext/archiver/bin/common.py
    # If not set (in cli access), patch the a translator with a mock, so the
    # _() functions in logic layer don't cause failure.
    from paste.registry import Registry
    from pylons import translator
    from ckan.lib.cli import MockTranslator
    if 'registery' not in globals():
        global registry
        registry = Registry()
        registry.prepare()

    if 'translator_obj' not in globals():
        global translator_obj
        translator_obj = MockTranslator()
        registry.register(translator, translator_obj)


def get_target():
    if hasattr(get_target, 'ckan'):
        return get_target.ckan
    ckan_url = config.get('ckan.syndicate.ckan_url')
    api_key = config.get('ckan.syndicate.api_key')
    user_agent = config.get('ckan.syndicate.user_agent', None)
    assert ckan_url and api_key, "Task must have ckan_url and api_key"

    ckan = ckanapi.RemoteCKAN(ckan_url, apikey=api_key, user_agent=user_agent)

    get_target.ckan = ckan
    return ckan


def filter_extras(extras):
    extras_dict = dict([(o['key'], o['value']) for o in extras])
    extras_dict.pop(get_syndicate_flag(), None)
    return [{'key': k, 'value': v} for (k, v) in extras_dict.iteritems()]


def filter_resources(resources):
    return [
        {'url': r['url'], 'name': r['name']} for r in resources
    ]


def sync_package(package_id, action, ckan_ini_filepath=None):
    logger.info('sync package {0}'.format(package_id))

    # load the package at run of time task (rather than use package state at
    # time of task creation).
    from ckan import model
    context = {'model': model, 'ignore_auth': True, 'session': model.Session,
               'use_cache': False, 'validate': False}

    params = {
        'id': package_id,
    }
    package = toolkit.get_action('package_show')(
        context,
        params,
    )

    if action == 'dataset/create':
        _create_package(package)

    elif action == 'dataset/update':
        _update_package(package)
    else:
        raise Exception('Unsupported action {0}'.format(action))


def replicate_remote_organization(org):
    ckan = get_target()

    try:
        remote_org = ckan.action.organization_show(id=org['name'])
    except toolkit.ObjectNotFound:
        org.pop('image_url')
        org.pop('id')
        remote_org = ckan.action.organization_create(**org)

    return remote_org['id']


def _create_package(package):
    ckan = get_target()

    # Create a new package based on the local instance
    new_package_data = dict(package)
    del new_package_data['id']

    new_package_data['name'] = "%s-%s" % (
        get_syndicated_name_prefix(),
        new_package_data['name'])

    new_package_data['extras'] = filter_extras(new_package_data['extras'])
    new_package_data['resources'] = filter_resources(package['resources'])

    org = new_package_data.pop('organization')

    if is_organization_preserved():
        org_id = replicate_remote_organization(org)
    else:
        org_id = get_syndicated_organization()

    new_package_data['owner_org'] = org_id

    try:
        # TODO: No automated test
        new_package_data = toolkit.get_action('update_dataset_for_syndication')(
            {}, {'dataset_dict': new_package_data})
    except KeyError:
        pass

    try:
        remote_package = ckan.action.package_create(**new_package_data)
        set_syndicated_id(package, remote_package['id'])
    except toolkit.ValidationError as e:
        if 'That URL is already in use.' in e.error_dict.get('name', []):
            logger.info("package with name '{0}' already exists. Check creator.".format(
                new_package_data['name']))
            author = get_syndicated_author()
            if author is None:
                raise
            try:
                remote_package = ckan.action.package_show(
                    id=new_package_data['name'])
                remote_user = ckan.action.user_show(id=author)
            except toolkit.ValidationError as e:
                log.error(e.errors)
                raise
            except toolkit.ObjectNotFound as e:
                log.error('User "{0}" not found'.format(author))
                raise
            else:
                if remote_package['creator_user_id'] == remote_user['id']:
                    logger.info("Author is the same({0}). Updating".format(
                        author))

                    ckan.action.package_update(
                        id=remote_package['id'],
                        **new_package_data
                    )
                    set_syndicated_id(package, remote_package['id'])
                else:
                    logger.info(
                        "Creator of remote package '{0}' did not match '{1}'. Skipping".format(
                            remote_user['name'], author))


def _update_package(package):
    syndicated_id = get_pkg_dict_extra(package, get_syndicated_id())

    if syndicated_id is None:
        _create_package(package)
        return

    ckan = get_target()

    try:
        updated_package = dict(package)
        # Keep the existing remote ID and Name
        del updated_package['id']
        del updated_package['name']

        updated_package['extras'] = filter_extras(package['extras'])
        updated_package['resources'] = filter_resources(package['resources'])

        org = updated_package.pop('organization')

        if is_organization_preserved():
            org_id = replicate_remote_organization(org)
        else:
            org_id = get_syndicated_organization()

        updated_package['owner_org'] = org_id

        try:
            # TODO: No automated test
            updated_package = toolkit.get_action(
                'update_dataset_for_syndication')(
                {}, {'dataset_dict': updated_package})
        except KeyError:
            pass

        ckan.action.package_update(
            id=syndicated_id,
            **updated_package
        )
    except ckanapi.NotFound:
        _create_package(package)


def set_syndicated_id(local_package, remote_package_id):
    """ Set the remote package id on the local package """
    extras = local_package['extras']
    extras_dict = dict([(o['key'], o['value']) for o in extras])
    extras_dict.update({get_syndicated_id(): remote_package_id})
    extras = [{'key': k, 'value': v} for (k, v) in extras_dict.iteritems()]
    local_package['extras'] = extras
    _update_package_extras(local_package)


def _update_package_extras(package):
    from ckan import model
    from ckan.lib.dictization.model_save import package_extras_save

    package_id = package['id']
    package_obj = model.Package.get(package_id)
    if not package:
        raise Exception('No Package with ID %s found:s' % package_id)

    extra_dicts = package.get("extras")
    context_ = {'model': model, 'session': model.Session}
    model.repo.new_revision()
    package_extras_save(extra_dicts, package_obj, context_)
    model.Session.commit()
    model.Session.flush()

    _update_search_index(package_obj.id, logger)


def _update_search_index(package_id, log):
    '''
    Tells CKAN to update its search index for a given package.
    '''
    from ckan import model
    from ckan.lib.search.index import PackageSearchIndex
    package_index = PackageSearchIndex()
    context_ = {'model': model, 'ignore_auth': True, 'session': model.Session,
                'use_cache': False, 'validate': False}
    package = toolkit.get_action('package_show')(context_, {'id': package_id})
    package_index.index_package(package, defer_commit=False)
    log.info('Search indexed %s', package['name'])
