
#
# Freesound is (c) MUSIC TECHNOLOGY GROUP, UNIVERSITAT POMPEU FABRA
#
# Freesound is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# Freesound is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Authors:
#     See AUTHORS file.
#

import datetime
import json
import logging
from collections import defaultdict

import re
from django.conf import settings
from django.shortcuts import render, redirect, reverse
from django.shortcuts import get_object_or_404
from django.http import JsonResponse

import forms
import sounds
import forum
from utils.search.search_general import search_prepare_sort, search_process_filter, \
    search_prepare_query, perform_solr_query, search_prepare_parameters
from utils.logging_filters import get_client_ip
from utils.search.solr import Solr, SolrQuery, SolrResponseInterpreter, \
    SolrResponseInterpreterPaginator, SolrException
from clustering.interface import cluster_sound_results

logger = logging.getLogger("search")


def search(request):
    query_params, tvars = search_prepare_parameters(request)
    
    # get sound ids of the requested cluster
    cluster_id = request.GET.get('cluster_id', "")
    in_ids = get_ids_in_cluster(request, cluster_id)
    query_params.update({'in_ids': in_ids})  # for added cluster facet filter

    query = search_prepare_query(**query_params)

    # pass the url query params for later sending it to the clustering engine
    url_query_params_string = request.META['QUERY_STRING']

    try:
        non_grouped_number_of_results, facets, paginator, page, docs = perform_solr_query(query, tvars['current_page'])
        resultids = [d.get("id") for d in docs]
        resultsounds = sounds.models.Sound.objects.bulk_query_id(resultids)
        allsounds = {}
        for s in resultsounds:
            allsounds[s.id] = s
        # allsounds will contain info from all the sounds returned by bulk_query_id. This should
        # be all sounds in docs, but if solr and db are not synchronised, it might happen that there
        # are ids in docs which are not found in bulk_query_id. To avoid problems we remove elements
        # in docs that have not been loaded in allsounds.
        docs = [doc for doc in docs if doc["id"] in allsounds]
        for d in docs:
            d["sound"] = allsounds[d["id"]]
        
        tvars.update({
            'paginator': paginator,
            'page': page,
            'docs': docs,
            'facets': facets,
            'non_grouped_number_of_results': non_grouped_number_of_results,
            'url_query_params_string': url_query_params_string,
        })

    except SolrException as e:
        logger.warning('Search error: query: %s error %s' % (query, e))
        tvars.update({'error_text': 'There was an error while searching, is your query correct?'})
    except Exception as e:
        logger.error('Could probably not connect to Solr - %s' % e)
        tvars.update({'error_text': 'The search server could not be reached, please try again later.'})

    # enables AJAX clustering call & html clustering facets rendering
    if settings.ENABLE_SEARCH_RESULTS_CLUSTERING:
        tvars.update({'clustering_on': True})

    if request.GET.get("ajax", "") != "1":
        return render(request, 'search/search.html', tvars)
    else:
        return render(request, 'search/search_ajax.html', tvars)


def get_ids_in_cluster(request, requested_cluster_id):
    if requested_cluster_id == "":
        return []
    else:
        requested_cluster_id = int(requested_cluster_id) - 1

        # results are cached in clustering_utilities, features are: 'audio_fs', 'audio_as', 'audio_fs_selected', 'tag'
        result = cluster_sound_results(request, 'audio_as')
        results = result['result']
        num_clusters = num_clusters = len(results) + 1

        sounds_from_requested_cluster = results[int(requested_cluster_id)]

        return sounds_from_requested_cluster


def cluster_sounds(request):
    # pass the url query params for later sending it to the clustering engine
    url_query_params_string = request.META['QUERY_STRING']
    url_query_params_string = re.sub(r"(&cluster_id=[0-9]*)", "", url_query_params_string)

    result = cluster_sound_results(request, 'audio_as')

    if result['finished']:
        if result['result'] is not None:
            results = result['result']
            num_clusters = num_clusters = len(results) + 1
        else:
             return JsonResponse({'status': 'failed'}, safe=False)
    elif result['error']:
        return JsonResponse({'status': 'failed'}, safe=False)
    else:
        return JsonResponse({'status': 'pending'}, safe=False)

    num_sounds_per_cluster = [len(cluster) for cluster in results]
    classes = {sound_id: cluster_id for cluster_id, cluster in enumerate(results) for sound_id in cluster}

    # label clusters using most occuring tags
    sound_instances = sounds.models.Sound.objects.bulk_query_id(map(int, classes.keys()))
    sound_tags = {sound.id: sound.get_sound_tags() for sound in sound_instances}
    cluster_tags = defaultdict(list)
    # query_terms = {t.lower() for t in query_params['search_query'].split(' ')}
    query_terms = {t.lower() for t in request.GET.get('q', '').split(' ')}
    for sound_id, tags in sound_tags.iteritems():
        cluster_tags[classes[str(sound_id)]] += [t.lower() for t in tags if t.lower() not in query_terms]
    cluster_tags_with_count = {k: sorted([(t, tags.count(t)) for t in set(tags)], 
                                         key=lambda x: x[1], reverse=True)
                               for k, tags in cluster_tags.iteritems()}
    cluster_most_occuring_tags = [' '.join(zip(*tags[:3])[0]) for tags in cluster_tags_with_count.values() if len(tags)>2]  # dict values sorted?!

    return render(request, 'search/clustering_facet.html', {
            'results': classes,
            'url_query_params_string': url_query_params_string,
            'cluster_id_num_results': zip(range(num_clusters), num_sounds_per_cluster, cluster_most_occuring_tags),
    })


def cluster_visualisation(request):
    url_query_params_string = request.META['QUERY_STRING']
    return render(request, 'search/clustered_graph.html', {
            'url_query_params_string': url_query_params_string,
    })


def return_clustered_graph(request):
    result = cluster_sound_results(request, 'audio_as')
    graph = result['graph']

    results = sounds.models.Sound.objects.bulk_query_id([int(node['id']) for node in graph['nodes']])
    preview_urls_name_tags_by_id = {s.id:(s.get_preview_abs_url()[21:],
                                          s.original_filename,
                                          ' '.join(s.get_sound_tags()),
                                          s.get_absolute_url()) for s in results}

    for node in graph['nodes']:
        node['url'] = preview_urls_name_tags_by_id[int(node['id'])][0]
        node['name'] = preview_urls_name_tags_by_id[int(node['id'])][1]
        node['tags'] = preview_urls_name_tags_by_id[int(node['id'])][2]
        node['sound_page_url'] = preview_urls_name_tags_by_id[int(node['id'])][3]

    return JsonResponse(json.dumps(graph), safe=False)


def search_forum(request):
    search_query = request.GET.get("q", "")
    filter_query = request.GET.get("f", "")
    try:
        current_page = int(request.GET.get("page", 1))
    except ValueError:
        current_page = 1
    current_forum_name_slug = request.GET.get("forum", "").strip()    # for context sensitive search
    if current_forum_name_slug:
        current_forum = get_object_or_404(forum.models.Forum.objects, name_slug=current_forum_name_slug)
    else:
        current_forum = None
    sort = ["thread_created desc"]

    # Parse advanced search options
    advanced_search = request.GET.get("advanced_search", "")
    date_from = request.GET.get("dt_from", "")
    try:
        df_parsed = datetime.datetime.strptime(date_from, "%Y-%m-%d")
        date_from_display = df_parsed.strftime("%d-%m-%Y")
    except ValueError:
        date_from = ""
        date_from_display = "Choose a Date"
    date_to = request.GET.get("dt_to", "")
    try:
        dt_parsed = datetime.datetime.strptime(date_to, "%Y-%m-%d")
        date_to_display = dt_parsed.strftime("%d-%m-%Y")
    except ValueError:
        date_to = ""
        date_to_display = "Choose a Date"

    if search_query.startswith("search in"):
        search_query = ""

    error = False
    error_text = ""
    paginator = None
    num_results = None
    page = None
    results = []
    if search_query.strip() != "" or filter_query:
        # add current forum
        if current_forum:
            filter_query += "forum_name_slug:" + current_forum.name_slug

        # add date range
        if advanced_search == "1" and date_from != "" or date_to != "":
            filter_query = __add_date_range(filter_query, date_from, date_to)

        query = SolrQuery()
        query.set_dismax_query(search_query, query_fields=[("thread_title", 4),
                                                           ("post_body", 3),
                                                           ("thread_author", 3),
                                                           ("post_author", 3),
                                                           ("forum_name", 2)])
        query.set_highlighting_options_default(field_list=["post_body"],
                                               fragment_size=200,
                                               alternate_field="post_body",  # TODO: revise this param
                                               require_field_match=False,
                                               pre="<strong>",
                                               post="</strong>")
        query.set_query_options(start=(current_page - 1) * settings.SOUNDS_PER_PAGE,
                                rows=settings.SOUNDS_PER_PAGE,
                                field_list=["id",
                                            "forum_name",
                                            "forum_name_slug",
                                            "thread_id",
                                            "thread_title",
                                            "thread_author",
                                            "thread_created",
                                            "post_body",
                                            "post_author",
                                            "post_created",
                                            "num_posts"],
                                filter_query=filter_query,
                                sort=sort)

        query.set_group_field("thread_title_grouped")
        query.set_group_options(group_limit=30)

        solr = Solr(settings.SOLR_FORUM_URL)

        try:
            results = SolrResponseInterpreter(solr.select(unicode(query)))
            paginator = SolrResponseInterpreterPaginator(results, settings.SOUNDS_PER_PAGE)
            num_results = paginator.count
            page = paginator.page(current_page)
            error = False
        except SolrException as e:
            logger.warning("search error: query: %s error %s" % (query, e))
            error = True
            error_text = 'There was an error while searching, is your query correct?'
        except Exception as e:
            logger.error("Could probably not connect to Solr - %s" % e)
            error = True
            error_text = 'The search server could not be reached, please try again later.'


    tvars = {
        'advanced_search': advanced_search,
        'current_forum': current_forum,
        'current_page': current_page,
        'date_from': date_from,
        'date_from_display': date_from_display,
        'date_to': date_to,
        'date_to_display': date_to_display,
        'error': error,
        'error_text': error_text,
        'filter_query': filter_query,
        'num_results': num_results,
        'page': page,
        'paginator': paginator,
        'search_query': search_query,
        'sort': sort,
        'results': results,
    }

    return render(request, 'search/search_forum.html', tvars)


def get_pack_tags(pack_obj):
    query = SolrQuery()
    query.set_dismax_query('')
    filter_query = 'username:\"%s\" pack:\"%s\"' % (pack_obj.user.username, pack_obj.name)
    query.set_query_options(field_list=["id"], filter_query=filter_query)
    query.add_facet_fields("tag")
    query.set_facet_options("tag", limit=20, mincount=1)
    try:
        solr = Solr(settings.SOLR_URL)
        results = SolrResponseInterpreter(solr.select(unicode(query)))
    except (SolrException, Exception) as e:
        #  TODO: do something here?
        return False
    return results.facets


def __add_date_range(filter_query, date_from, date_to):
    if filter_query != "":
        filter_query += " "
    filter_query += "thread_created:["
    date_from = date_from + "T00:00:00Z" if date_from != "" else "*"
    date_to = date_to + "T00:00:00Z]" if date_to != "" else "*]"
    return filter_query + date_from + " TO " + date_to