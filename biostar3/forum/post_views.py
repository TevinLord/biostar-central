from __future__ import absolute_import, division, print_function, unicode_literals

# Python modules.
import logging
from collections import OrderedDict, defaultdict

# Django specific modules.
from django.shortcuts import render, redirect
from django.contrib.auth import get_user_model
from django.conf import settings
from django.contrib import messages
from django.core.urlresolvers import reverse
from django.db.models import Q, F
from django.contrib.auth.decorators import login_required

from taggit.models import TaggedItem, Tag

# Biostar specific local modules.
from . import models, query, search, auth
from .models import Vote, Post, PostView, UserGroup

logger = logging.getLogger('biostar')

# Get custom user model.
User = get_user_model()


def tag_list(request):
    template_name = "tag_list.html"

    # Takes a search parameter
    q = request.GET.get('q', '')
    if q:
        tags = Tag.objects.filter(name__icontains=q)
    else:
        tags = Tag.objects.all()

    tags = tags.order_by("name")

    paginator = query.TagPaginator(request, tags, per_page=100)
    page = paginator.curr_page()

    html_title = "Tags"

    context = dict(page=page, tags=page.object_list, html_title=html_title, q=q)
    return render(request, template_name, context)


def tag_filter(request, name):
    """
    Returns a list of posts filtered by a tag name.
    """
    posts = query.get_toplevel_posts(user=request.user, group=request.group)
    names = name.split("+")
    posts = posts.filter(tags__name__in=names)
    messages.info(request, 'Filtering for tags: %s' % name)
    return post_list(request, posts=posts)


@auth.valid_user
def posts_by_user(request, pk, user=None):
    """
    Returns the posts created by a user.
    """
    posts = query.get_all_posts(user=user, group=request.group)
    messages.info(request, 'Posts by: %s' % user.name)
    return post_list(request, posts=posts)


@auth.valid_user
def upvoted_posts(request, pk, user=None):
    """
    Returns the upvoted posts created by a user.
    """
    posts = query.get_posts_by_vote(user=user, group=request.group, vote_types=[Vote.BOOKMARK, Vote.UP])
    messages.info(request, 'Upvoted posts by: %s' % user.name)
    return post_list(request, posts=posts)


@login_required
def my_bookmarks(request):
    """
    Returns the bookmarks by a user.
    """
    user = request.user
    posts = query.get_my_bookmarks(user=user, group=request.group)
    messages.info(request, 'Bookmarks for: %s' % user.name)
    return post_list(request, posts=posts)


def post_list(request, posts=None):
    template_name = "post_list.html"

    if posts is None:
        # The view is generic and could be called prefilled with posts.
        posts = query.get_toplevel_posts(user=request.user, group=request.group)

    paginator = query.PostPaginator(request, posts, per_page=settings.POSTS_PER_PAGE, orphans=False)
    page = paginator.curr_page()

    # Add the recent votes
    recent_votes = query.recent_votes()
    html_title = "Post List"
    context = dict(page=page, posts=page.object_list, recent_votes=recent_votes, html_title=html_title)

    return render(request, template_name, context)

def group_list(request):
    template_name = "post_list.html"
    public_groups = UserGroup.objects.filter(public=True)

    context = dict(public_groups=public_groups)
    return render(request, template_name, context)

def search_results(request):
    """
    Produces the search results
    """
    template_name = "post_search_results.html"
    q = request.GET.get('q', '')

    if not q:
        return redirect(reverse("home"))

    posts = search.plain(q)

    paginator = query.PostPaginator(request, posts, per_page=settings.POSTS_PER_PAGE, orphans=False)
    page = paginator.curr_page()

    # Add the recent votes
    recent_votes = query.recent_votes()
    html_title = "Post List"
    context = dict(page=page, posts=page.object_list, recent_votes=recent_votes, html_title=html_title, q=q)

    return render(request, template_name, context)


def update_post_views(request, post, minutes=settings.POST_VIEW_INTERVAL):
    """
    Views are updated per user session"
    """
    ip = auth.remote_ip(request)
    since = auth.ago(minutes=minutes)
    try:
        # One view per time interval from each IP address.
        if not PostView.objects.filter(ip=ip, post=post, date__gt=since):
            PostView.objects.create(ip=ip, post=post, date=auth.now())
            Post.objects.filter(id=post.id).update(view_count=F('view_count') + 1)
    except Exception, exc:
        # Triggers if the IP address is spoofed and/or malformed.
        logger.error(exc)


@auth.valid_post
def post_view(request, pk, post=None, user=None):
    """
    Generates the page that contains a full thread.
    """

    template_name = "post_detail.html"

    if not post.is_toplevel:
        # Post is not at top level. Redirect and and scroll the page to the right anchor.
        return redirect(post.get_absolute_url())

    # Update post views
    update_post_views(request=request, post=post)

    # Gets all objects in a thread. Moderators get deleted objects as well.
    thread = [px for px in query.get_thread(post, user)]

    # Collect votes for authenticated users
    store = {Vote.UP: set(), Vote.BOOKMARK: set()}

    if user.is_authenticated():
        # Authenticated users have votes only.
        pids = [p.id for p in thread]
        votes = Vote.objects.filter(post_id__in=pids, author=user).values_list("post_id", "type")
        for post_id, vote_type in votes:
            store.setdefault(vote_type, set()).add(post_id)

    # Extra attributes carry context into templates.
    post.upvotes = store[Vote.UP]
    post.bookmarks = store[Vote.BOOKMARK]

    # Set up additional attributes on each post
    write_access_check = auth.thread_write_access(user=user, root=post)

    def decorator(p):
        # Each post needs to carry information on its status relative to the user.
        p.editable = write_access_check(user=user, post=p)
        p.has_vote = p.id in post.upvotes
        p.has_bookmark = p.id in post.bookmarks
        return p

    # Decorate all posts in the thread.
    thread = map(decorator, thread)

    # Decorate the main post as well.
    post = decorator(post)

    # Store answers in a separate list for simpler access.
    post.answers = filter(lambda p: p.type == models.Post.ANSWER, thread)

    # Comments will be stored in a dictionary for fast access.
    comment_list = filter(lambda pc: pc.type == models.Post.COMMENT, thread)

    # Collect comments into a dictionary keyed by the parent id with
    # comments as a value list
    post.comments = OrderedDict()
    for comment in comment_list:
        post.comments.setdefault(comment.parent.id, []).append(comment)

    if user.is_authenticated():
        # This is for testing only. Keeps adding comments to posts on the page.
        import random, faker

        f = faker.Factory.create()
        u = random.choice(User.objects.all())
        parent = random.choice(thread + [post])
        text = f.bs()
        comment = models.Post.objects.create(type=models.Post.COMMENT, parent=parent, content=text, author=u)

    # Add object to the context.
    html_title = post.title
    context = dict(post=post, html_title=html_title)

    return render(request, template_name, context)

