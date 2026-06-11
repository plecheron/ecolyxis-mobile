from flask import request, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import Workspace, Thread
from . import workspace_bp
from .summaries import generate_thread_summary

import logging
logger = logging.getLogger("ecolyxis.workspace.routes")


@workspace_bp.route('', methods=['GET'])
@login_required
def list_workspaces():
    """List all workspaces for the current user with thread counts."""
    workspaces = Workspace.query.filter_by(user_id=current_user.id)\
        .order_by(Workspace.updated_at.desc()).all()
    return jsonify([{
        'id': w.id,
        'name': w.name,
        'description': w.description,
        'created_at': w.created_at.isoformat() if w.created_at else None,
        'updated_at': w.updated_at.isoformat() if w.updated_at else None,
        'thread_count': w.threads.count(),
    } for w in workspaces])


@workspace_bp.route('', methods=['POST'])
@login_required
def create_workspace():
    """Create a new workspace."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body required'}), 400
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400

    existing = Workspace.query.filter_by(user_id=current_user.id, name=name).first()
    if existing:
        return jsonify({'error': 'Workspace with this name already exists'}), 409

    w = Workspace(user_id=current_user.id, name=name, description=data.get('description'))
    db.session.add(w)
    db.session.commit()
    return jsonify({'id': w.id, 'name': w.name, 'description': w.description}), 201


@workspace_bp.route('/<workspace_id>', methods=['GET'])
@login_required
def get_workspace(workspace_id):
    """Get a single workspace with its threads."""
    w = Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()
    threads = w.threads.order_by(Thread.updated_at.desc()).all()
    return jsonify({
        'id': w.id,
        'name': w.name,
        'description': w.description,
        'created_at': w.created_at.isoformat() if w.created_at else None,
        'updated_at': w.updated_at.isoformat() if w.updated_at else None,
        'thread_count': w.threads.count(),
        'threads': [{
            'id': t.id,
            'title': t.title,
            'summary': t.summary,
            'updated_at': t.updated_at.isoformat() if t.updated_at else None,
            'message_count': t.messages.count(),
        } for t in threads],
    })


@workspace_bp.route('/<workspace_id>', methods=['PATCH'])
@login_required
def update_workspace(workspace_id):
    """Rename or update description of a workspace."""
    w = Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body required'}), 400
    if 'name' in data:
        name = data['name'].strip()
        if not name:
            return jsonify({'error': 'Name cannot be empty'}), 400
        existing = Workspace.query.filter_by(user_id=current_user.id, name=name).first()
        if existing and existing.id != w.id:
            return jsonify({'error': 'Workspace with this name already exists'}), 409
        w.name = name
    if 'description' in data:
        w.description = data['description']
    db.session.commit()
    return jsonify({'id': w.id, 'name': w.name, 'description': w.description})


@workspace_bp.route('/<workspace_id>', methods=['DELETE'])
@login_required
def delete_workspace(workspace_id):
    """Delete a workspace. Threads become unassigned."""
    w = Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()
    Thread.query.filter_by(workspace_id=w.id).update({'workspace_id': None})
    db.session.delete(w)
    db.session.commit()
    return jsonify({'success': True})


@workspace_bp.route('/<workspace_id>/threads', methods=['GET'])
@login_required
def list_workspace_threads(workspace_id):
    """List threads in a workspace."""
    w = Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()
    threads = w.threads.order_by(Thread.updated_at.desc()).all()
    return jsonify([{
        'id': t.id,
        'title': t.title,
        'summary': t.summary,
        'updated_at': t.updated_at.isoformat() if t.updated_at else None,
        'message_count': t.messages.count(),
    } for t in threads])


@workspace_bp.route('/<workspace_id>/threads/<thread_id>', methods=['PUT'])
@login_required
def assign_thread_to_workspace(workspace_id, thread_id):
    """Assign or move a thread to a workspace.

    After assigning, generates a summary for the assigned thread and also
    generates summaries for any other threads in the workspace that don't
    yet have one.
    """
    w = Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()
    t = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    t.workspace_id = w.id
    db.session.commit()

    # Generate summary for the assigned thread
    summary = None
    try:
        summary = generate_thread_summary(t)
    except Exception as e:
        logger.warning("Failed to generate summary for thread %s: %s", t.id, e)

    # Generate summaries for other threads in the workspace that lack one
    try:
        other_threads = (
            Thread.query
            .filter(
                Thread.workspace_id == w.id,
                Thread.id != t.id,
                Thread.summary.is_(None),
            )
            .all()
        )
        for ot in other_threads:
            try:
                generate_thread_summary(ot)
            except Exception as e:
                logger.warning("Failed to generate summary for thread %s: %s", ot.id, e)
    except Exception as e:
        logger.warning("Error generating summaries for other threads: %s", e)

    return jsonify({
        'success': True,
        'summary': summary,
    })


@workspace_bp.route('/threads/<thread_id>', methods=['DELETE'])
@login_required
def unassign_thread(thread_id):
    """Remove a thread from its workspace."""
    t = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    t.workspace_id = None
    db.session.commit()
    return jsonify({'success': True})


@workspace_bp.route('/threads/<thread_id>/summarize', methods=['POST'])
@login_required
def summarize_thread(thread_id):
    """Explicitly generate (or regenerate) a summary for a thread."""
    t = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    summary = generate_thread_summary(t)
    if summary:
        return jsonify({'success': True, 'summary': summary})
    return jsonify({'error': 'Could not generate summary'}), 500


@workspace_bp.route('/threads/<thread_id>/toggle-context', methods=['POST'])
@login_required
def toggle_workspace_context(thread_id):
    """Toggle use_workspace_context on a thread."""
    t = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    t.use_workspace_context = not t.use_workspace_context
    db.session.commit()
    return jsonify({'success': True, 'use_workspace_context': t.use_workspace_context})
