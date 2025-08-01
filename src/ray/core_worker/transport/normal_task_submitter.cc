// Copyright 2017 The Ray Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//  http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "ray/core_worker/transport/normal_task_submitter.h"

#include <deque>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "ray/gcs/pb_util.h"

namespace ray {
namespace core {

Status NormalTaskSubmitter::SubmitTask(TaskSpecification task_spec) {
  RAY_CHECK(task_spec.IsNormalTask());
  RAY_LOG(DEBUG) << "Submit task " << task_spec.TaskId();
  num_tasks_submitted_.fetch_add(1, std::memory_order_relaxed);

  resolver_.ResolveDependencies(task_spec, [this, task_spec](Status status) mutable {
    // NOTE: task_spec here is capture copied (from a stack variable) and also
    // mutable. (Mutations to the variable are expected to be shared inside and
    // outside of this closure).
    task_manager_.MarkDependenciesResolved(task_spec.TaskId());
    if (!status.ok()) {
      // TODO(https://github.com/ray-project/ray/issues/54871): There is a potential
      // logical race conditions here where the task is cancelled right before the
      // task is retried. Task cancellation might remove the task from the submissible
      // task queue, while the task retry here expects that the task must be in the
      // submissible task queue.
      RAY_LOG(WARNING) << "Resolving task dependencies failed " << status.ToString();
      bool will_retry = task_manager_.FailOrRetryPendingTask(
          task_spec.TaskId(), rpc::ErrorType::DEPENDENCY_RESOLUTION_FAILED, &status);
      if (!will_retry) {
        absl::MutexLock lock(&mu_);
        cancelled_tasks_.erase(task_spec.TaskId());
      }
      return;
    }
    RAY_LOG(DEBUG) << "Task dependencies resolved " << task_spec.TaskId();

    absl::MutexLock lock(&mu_);
    if (cancelled_tasks_.erase(task_spec.TaskId()) > 0) {
      task_manager_.FailPendingTask(task_spec.TaskId(), rpc::ErrorType::TASK_CANCELLED);
      return;
    }

    task_spec.GetMutableMessage().set_dependency_resolution_timestamp_ms(
        current_sys_time_ms());
    // Note that the dependencies in the task spec are mutated to only contain
    // plasma dependencies after ResolveDependencies finishes.
    const SchedulingKey scheduling_key(
        task_spec.GetSchedulingClass(),
        task_spec.GetDependencyIds(),
        task_spec.IsActorCreationTask() ? task_spec.ActorCreationId() : ActorID::Nil(),
        task_spec.GetRuntimeEnvHash());
    auto &scheduling_key_entry = scheduling_key_entries_[scheduling_key];
    scheduling_key_entry.task_queue.push_back(task_spec);
    scheduling_key_entry.resource_spec = std::move(task_spec);

    if (!scheduling_key_entry.AllWorkersBusy()) {
      // There are idle workers, so we don't need more
      // workers.
      for (const auto &active_worker_addr : scheduling_key_entry.active_workers) {
        auto iter = worker_to_lease_entry_.find(active_worker_addr);
        RAY_CHECK(iter != worker_to_lease_entry_.end());
        auto &lease_entry = iter->second;
        if (!lease_entry.is_busy) {
          OnWorkerIdle(active_worker_addr,
                       scheduling_key,
                       /*was_error*/ false,
                       /*error_detail*/ "",
                       /*worker_exiting*/ false,
                       lease_entry.assigned_resources);
          break;
        }
      }
    }
    RequestNewWorkerIfNeeded(scheduling_key);
  });
  return Status::OK();
}

void NormalTaskSubmitter::AddWorkerLeaseClient(
    const rpc::Address &addr,
    std::shared_ptr<RayletClientInterface> lease_client,
    const google::protobuf::RepeatedPtrField<rpc::ResourceMapEntry> &assigned_resources,
    const SchedulingKey &scheduling_key,
    const TaskID &task_id) {
  core_worker_client_pool_->GetOrConnect(addr);
  int64_t expiration = current_time_ms() + lease_timeout_ms_;
  LeaseEntry new_lease_entry{
      std::move(lease_client), expiration, assigned_resources, scheduling_key, task_id};
  worker_to_lease_entry_.emplace(addr, new_lease_entry);

  auto &scheduling_key_entry = scheduling_key_entries_[scheduling_key];
  RAY_CHECK(scheduling_key_entry.active_workers.emplace(addr).second);
  RAY_CHECK(scheduling_key_entry.active_workers.size() >= 1);
}

void NormalTaskSubmitter::ReturnWorker(const rpc::Address &addr,
                                       bool was_error,
                                       const std::string &error_detail,
                                       bool worker_exiting,
                                       const SchedulingKey &scheduling_key) {
  RAY_LOG(DEBUG) << "Returning worker " << WorkerID::FromBinary(addr.worker_id())
                 << " to raylet " << NodeID::FromBinary(addr.raylet_id());
  auto &scheduling_key_entry = scheduling_key_entries_[scheduling_key];
  RAY_CHECK(scheduling_key_entry.active_workers.size() >= 1);
  auto &lease_entry = worker_to_lease_entry_[addr];
  RAY_CHECK(lease_entry.lease_client);
  RAY_CHECK(!lease_entry.is_busy);

  // Decrement the number of active workers consuming tasks from the queue associated
  // with the current scheduling_key
  scheduling_key_entry.active_workers.erase(addr);
  if (scheduling_key_entry.CanDelete()) {
    // We can safely remove the entry keyed by scheduling_key from the
    // scheduling_key_entries_ hashmap.
    scheduling_key_entries_.erase(scheduling_key);
  }

  auto status =
      lease_entry.lease_client->ReturnWorker(addr.port(),
                                             WorkerID::FromBinary(addr.worker_id()),
                                             was_error,
                                             error_detail,
                                             worker_exiting);
  if (!status.ok()) {
    RAY_LOG(ERROR) << "Error returning worker to raylet: " << status.ToString();
  }
  worker_to_lease_entry_.erase(addr);
}

void NormalTaskSubmitter::OnWorkerIdle(
    const rpc::Address &addr,
    const SchedulingKey &scheduling_key,
    bool was_error,
    const std::string &error_detail,
    bool worker_exiting,
    const google::protobuf::RepeatedPtrField<rpc::ResourceMapEntry> &assigned_resources) {
  auto &lease_entry = worker_to_lease_entry_[addr];
  if (!lease_entry.lease_client) {
    return;
  }

  auto &scheduling_key_entry = scheduling_key_entries_[scheduling_key];
  auto &current_queue = scheduling_key_entry.task_queue;
  // Return the worker if there was an error executing the previous task,
  // the lease is expired; Return the worker if there are no more applicable
  // queued tasks.
  if ((was_error || worker_exiting ||
       current_time_ms() > lease_entry.lease_expiration_time) ||
      current_queue.empty()) {
    RAY_CHECK(scheduling_key_entry.active_workers.size() >= 1);

    // Return the worker only if there are no tasks to do.
    if (!lease_entry.is_busy) {
      ReturnWorker(addr, was_error, error_detail, worker_exiting, scheduling_key);
    }
  } else {
    auto client = core_worker_client_pool_->GetOrConnect(addr);

    while (!current_queue.empty() && !lease_entry.is_busy) {
      auto task_spec = std::move(current_queue.front());
      current_queue.pop_front();

      lease_entry.is_busy = true;

      // Increment the total number of tasks in flight to any worker associated with the
      // current scheduling_key
      RAY_CHECK(scheduling_key_entry.active_workers.size() >= 1);
      scheduling_key_entry.num_busy_workers++;

      task_spec.GetMutableMessage().set_lease_grant_timestamp_ms(current_sys_time_ms());
      task_spec.EmitTaskMetrics();

      executing_tasks_.emplace(task_spec.TaskId(), addr);
      PushNormalTask(
          addr, client, scheduling_key, std::move(task_spec), assigned_resources);
    }

    CancelWorkerLeaseIfNeeded(scheduling_key);
  }
  RequestNewWorkerIfNeeded(scheduling_key);
}

void NormalTaskSubmitter::CancelWorkerLeaseIfNeeded(const SchedulingKey &scheduling_key) {
  auto &scheduling_key_entry = scheduling_key_entries_[scheduling_key];
  auto &task_queue = scheduling_key_entry.task_queue;
  if (!task_queue.empty()) {
    // There are still pending tasks so let the worker lease request succeed.
    return;
  }

  RAY_LOG(DEBUG) << "Task queue is empty; canceling lease request";

  for (auto &pending_lease_request : scheduling_key_entry.pending_lease_requests) {
    // There is an in-flight lease request. Cancel it.
    auto lease_client = GetOrConnectLeaseClient(&pending_lease_request.second);
    auto &task_id = pending_lease_request.first;
    RAY_LOG(DEBUG) << "Canceling lease request " << task_id;
    lease_client->CancelWorkerLease(
        task_id,
        [this, scheduling_key](const Status &status,
                               const rpc::CancelWorkerLeaseReply &reply) {
          absl::MutexLock lock(&mu_);
          if (status.ok() && !reply.success()) {
            // The cancellation request can fail if the raylet does not have
            // the request queued. This can happen if: a) due to message
            // reordering, the raylet has not yet received the worker lease
            // request, or b) we have already returned the worker lease
            // request. In the former case, we should try the cancellation
            // request again. In the latter case, the in-flight lease request
            // should already have been removed from our local state, so we no
            // longer need to cancel.
            CancelWorkerLeaseIfNeeded(scheduling_key);
          }
        });
  }
}

std::shared_ptr<RayletClientInterface> NormalTaskSubmitter::GetOrConnectLeaseClient(
    const rpc::Address *raylet_address) {
  std::shared_ptr<RayletClientInterface> lease_client;
  RAY_CHECK(raylet_address != nullptr);
  if (NodeID::FromBinary(raylet_address->raylet_id()) != local_raylet_id_) {
    // A remote raylet was specified. Connect to the raylet if needed.
    NodeID raylet_id = NodeID::FromBinary(raylet_address->raylet_id());
    auto it = remote_lease_clients_.find(raylet_id);
    if (it == remote_lease_clients_.end()) {
      RAY_LOG(INFO) << "Connecting to raylet " << raylet_id;
      it = remote_lease_clients_
               .emplace(raylet_id,
                        raylet_client_pool_->GetOrConnectByAddress(*raylet_address))
               .first;
    }
    lease_client = it->second;
  } else {
    lease_client = local_lease_client_;
  }

  return lease_client;
}

void NormalTaskSubmitter::ReportWorkerBacklog() {
  absl::MutexLock lock(&mu_);
  ReportWorkerBacklogInternal();
}

void NormalTaskSubmitter::ReportWorkerBacklogInternal() {
  absl::flat_hash_map<SchedulingClass, std::pair<TaskSpecification, int64_t>> backlogs;
  for (auto &scheduling_key_and_entry : scheduling_key_entries_) {
    const SchedulingClass scheduling_class = std::get<0>(scheduling_key_and_entry.first);
    if (backlogs.find(scheduling_class) == backlogs.end()) {
      backlogs[scheduling_class].first = scheduling_key_and_entry.second.resource_spec;
      backlogs[scheduling_class].second = 0;
    }
    // We report backlog size per scheduling class not per scheduling key
    // so we need to aggregate backlog sizes of different scheduling keys
    // with the same scheduling class
    backlogs[scheduling_class].second += scheduling_key_and_entry.second.BacklogSize();
    scheduling_key_and_entry.second.last_reported_backlog_size =
        scheduling_key_and_entry.second.BacklogSize();
  }

  std::vector<rpc::WorkerBacklogReport> backlog_reports;
  for (const auto &backlog : backlogs) {
    rpc::WorkerBacklogReport backlog_report;
    backlog_report.mutable_resource_spec()->CopyFrom(backlog.second.first.GetMessage());
    backlog_report.set_backlog_size(backlog.second.second);
    backlog_reports.emplace_back(backlog_report);
  }
  local_lease_client_->ReportWorkerBacklog(WorkerID::FromBinary(rpc_address_.worker_id()),
                                           backlog_reports);
}

void NormalTaskSubmitter::ReportWorkerBacklogIfNeeded(
    const SchedulingKey &scheduling_key) {
  const auto &scheduling_key_entry = scheduling_key_entries_[scheduling_key];

  if (scheduling_key_entry.last_reported_backlog_size !=
      scheduling_key_entry.BacklogSize()) {
    ReportWorkerBacklogInternal();
  }
}

void NormalTaskSubmitter::RequestNewWorkerIfNeeded(const SchedulingKey &scheduling_key,
                                                   const rpc::Address *raylet_address) {
  auto &scheduling_key_entry = scheduling_key_entries_[scheduling_key];

  const size_t kMaxPendingLeaseRequestsPerSchedulingCategory =
      lease_request_rate_limiter_->GetMaxPendingLeaseRequestsPerSchedulingCategory();

  if (scheduling_key_entry.pending_lease_requests.size() >=
      kMaxPendingLeaseRequestsPerSchedulingCategory) {
    RAY_LOG(DEBUG) << "Exceeding the pending request limit "
                   << kMaxPendingLeaseRequestsPerSchedulingCategory;
    return;
  }

  if (!scheduling_key_entry.AllWorkersBusy()) {
    // There are idle workers, so we don't need more.
    return;
  }

  const auto &task_queue = scheduling_key_entry.task_queue;
  if (task_queue.empty()) {
    if (scheduling_key_entry.CanDelete()) {
      // We can safely remove the entry keyed by scheduling_key from the
      // scheduling_key_entries_ hashmap.
      scheduling_key_entries_.erase(scheduling_key);
    }
    return;
  } else if (scheduling_key_entry.task_queue.size() <=
             scheduling_key_entry.pending_lease_requests.size()) {
    // All tasks have corresponding pending leases, no need to request more
    return;
  }

  num_leases_requested_++;
  // Create a TaskSpecification with an overwritten TaskID to make sure we don't reuse the
  // same TaskID to request a worker
  auto resource_spec_msg = scheduling_key_entry.resource_spec.GetMutableMessage();
  resource_spec_msg.set_task_id(TaskID::FromRandom(job_id_).Binary());
  const TaskSpecification resource_spec = TaskSpecification(std::move(resource_spec_msg));
  rpc::Address best_node_address;
  const bool is_spillback = (raylet_address != nullptr);
  bool is_selected_based_on_locality = false;
  if (raylet_address == nullptr) {
    // If no raylet address is given, find the best worker for our next lease request.
    std::tie(best_node_address, is_selected_based_on_locality) =
        lease_policy_->GetBestNodeForTask(resource_spec);
    raylet_address = &best_node_address;
  }

  auto lease_client = GetOrConnectLeaseClient(raylet_address);
  const TaskID task_id = resource_spec.TaskId();
  const std::string task_name = resource_spec.GetName();
  RAY_LOG(DEBUG) << "Requesting lease from raylet "
                 << NodeID::FromBinary(raylet_address->raylet_id()) << " for task "
                 << task_id;

  lease_client->RequestWorkerLease(
      resource_spec.GetMessage(),
      /*grant_or_reject=*/is_spillback,
      [this,
       scheduling_key,
       task_id,
       task_name,
       is_spillback,
       raylet_address = *raylet_address](const Status &status,
                                         const rpc::RequestWorkerLeaseReply &reply) {
        std::deque<TaskSpecification> tasks_to_fail;
        rpc::RayErrorInfo error_info;
        ray::Status error_status;
        rpc::ErrorType error_type = rpc::ErrorType::WORKER_DIED;
        {
          absl::MutexLock lock(&mu_);

          auto &scheduling_key_entry = scheduling_key_entries_[scheduling_key];
          auto lease_client = GetOrConnectLeaseClient(&raylet_address);
          scheduling_key_entry.pending_lease_requests.erase(task_id);

          if (status.ok()) {
            if (reply.canceled()) {
              RAY_LOG(DEBUG) << "Lease canceled for task: " << task_id
                             << ", canceled type: "
                             << rpc::RequestWorkerLeaseReply::SchedulingFailureType_Name(
                                    reply.failure_type());
              if (reply.failure_type() ==
                      rpc::RequestWorkerLeaseReply::
                          SCHEDULING_CANCELLED_RUNTIME_ENV_SETUP_FAILED ||
                  reply.failure_type() ==
                      rpc::RequestWorkerLeaseReply::
                          SCHEDULING_CANCELLED_PLACEMENT_GROUP_REMOVED ||
                  reply.failure_type() ==
                      rpc::RequestWorkerLeaseReply::SCHEDULING_CANCELLED_UNSCHEDULABLE) {
                // We need to actively fail all of the pending tasks in the queue when the
                // placement group was removed or the runtime env failed to be set up.
                // Such an operation is straightforward for the scenario of placement
                // group removal as all tasks in the queue are associated with the same
                // placement group, but in the case of runtime env setup failed, This
                // makes an implicit assumption that runtime_env failures are not
                // transient -- we may consider adding some retries in the future.
                if (reply.failure_type() ==
                    rpc::RequestWorkerLeaseReply::
                        SCHEDULING_CANCELLED_RUNTIME_ENV_SETUP_FAILED) {
                  error_type = rpc::ErrorType::RUNTIME_ENV_SETUP_FAILED;
                  error_info.mutable_runtime_env_setup_failed_error()->set_error_message(
                      reply.scheduling_failure_message());
                } else if (reply.failure_type() ==
                           rpc::RequestWorkerLeaseReply::
                               SCHEDULING_CANCELLED_UNSCHEDULABLE) {
                  error_type = rpc::ErrorType::TASK_UNSCHEDULABLE_ERROR;
                } else {
                  error_type = rpc::ErrorType::TASK_PLACEMENT_GROUP_REMOVED;
                }
                error_info.set_error_message(
                    absl::StrCat(reply.scheduling_failure_message(),
                                 " task_id=",
                                 task_id.Hex(),
                                 ", task_name=",
                                 task_name));

                tasks_to_fail = std::move(scheduling_key_entry.task_queue);
                scheduling_key_entry.task_queue.clear();
                if (scheduling_key_entry.CanDelete()) {
                  scheduling_key_entries_.erase(scheduling_key);
                }
              } else {
                RequestNewWorkerIfNeeded(scheduling_key);
              }
            } else if (reply.rejected()) {
              RAY_LOG(DEBUG) << "Lease rejected " << task_id;
              // It might happen when the first raylet has a stale view
              // of the spillback raylet resources.
              // Retry the request at the first raylet since the resource view may be
              // refreshed.
              RAY_CHECK(is_spillback);
              RequestNewWorkerIfNeeded(scheduling_key);
            } else if (!reply.worker_address().raylet_id().empty()) {
              // We got a lease for a worker. Add the lease client state and try to
              // assign work to the worker.
              RAY_LOG(DEBUG) << "Lease granted to task " << task_id << " from raylet "
                             << NodeID::FromBinary(reply.worker_address().raylet_id())
                             << " with worker "
                             << WorkerID::FromBinary(reply.worker_address().worker_id());

              AddWorkerLeaseClient(reply.worker_address(),
                                   std::move(lease_client),
                                   reply.resource_mapping(),
                                   scheduling_key,
                                   task_id);
              RAY_CHECK(scheduling_key_entry.active_workers.size() >= 1);
              OnWorkerIdle(reply.worker_address(),
                           scheduling_key,
                           /*was_error=*/false,
                           /*error_detail*/ "",
                           /*worker_exiting=*/false,
                           reply.resource_mapping());
            } else {
              // The raylet redirected us to a different raylet to retry at.
              RAY_CHECK(!is_spillback);
              RAY_LOG(DEBUG) << "Redirect lease for task " << task_id << " from raylet "
                             << NodeID::FromBinary(raylet_address.raylet_id())
                             << " to raylet "
                             << NodeID::FromBinary(
                                    reply.retry_at_raylet_address().raylet_id());

              RequestNewWorkerIfNeeded(scheduling_key, &reply.retry_at_raylet_address());
            }
          } else if (lease_client != local_lease_client_) {
            // A lease request to a remote raylet failed. Retry locally if the lease is
            // still needed.
            // TODO(swang): Fail after some number of retries?
            RAY_LOG_EVERY_MS(INFO, 30 * 1000)
                << "Retrying attempt to schedule task (id: " << task_id
                << " name: " << task_name
                << ") at remote node (id: " << raylet_address.raylet_id()
                << " ip: " << raylet_address.ip_address()
                << "). Try again "
                   "on a local node. Error: "
                << status.ToString();

            RequestNewWorkerIfNeeded(scheduling_key);

          } else {
            if (status.IsRpcError() &&
                status.rpc_code() == grpc::StatusCode::UNAVAILABLE) {
              RAY_LOG(WARNING)
                  << "The worker failed to receive a response from the local "
                  << "raylet because the raylet is unavailable (crashed). "
                  << "Error: " << status;
              if (worker_type_ == WorkerType::WORKER) {
                // Exit the worker so that caller can retry somewhere else.
                RAY_LOG(WARNING) << "Terminating the worker due to local raylet death";
                QuickExit();
              }
              RAY_CHECK(worker_type_ == WorkerType::DRIVER);
              error_type = rpc::ErrorType::LOCAL_RAYLET_DIED;
              error_status = status;
              // Grpc errors are not helpful at all. So we are overwriting it.
              std::stringstream ss;
              ss << "The worker failed to receive a response from the local raylet"
                 << "(id: " << NodeID::FromBinary(raylet_address.raylet_id()).Hex()
                 << " ,ip: " << raylet_address.ip_address() << ") "
                 << "because the raylet is "
                    "unavailable (crashed).";
              error_info.set_error_message(ss.str());
              tasks_to_fail = std::move(scheduling_key_entry.task_queue);
              scheduling_key_entry.task_queue.clear();
              if (scheduling_key_entry.CanDelete()) {
                scheduling_key_entries_.erase(scheduling_key);
              }
            } else {
              RAY_LOG(WARNING)
                  << "The worker failed to receive a response from the local raylet, but "
                     "raylet is still alive. Try again on a local node. Error: "
                  << status;
              // TODO(sang): Maybe we should raise FATAL error if it happens too many
              // times.
              RequestNewWorkerIfNeeded(scheduling_key);
            }
          }
        }
        error_info.set_error_type(error_type);
        while (!tasks_to_fail.empty()) {
          auto &task_spec = tasks_to_fail.front();
          if (task_spec.IsActorCreationTask() &&
              error_type == rpc::ErrorType::TASK_PLACEMENT_GROUP_REMOVED) {
            task_manager_.FailPendingTask(task_spec.TaskId(),
                                          rpc::ErrorType::ACTOR_PLACEMENT_GROUP_REMOVED,
                                          &error_status,
                                          &error_info);
          } else {
            task_manager_.FailPendingTask(
                task_spec.TaskId(), error_type, &error_status, &error_info);
          }
          tasks_to_fail.pop_front();
        }
      },
      task_queue.size(),
      is_selected_based_on_locality);
  scheduling_key_entry.pending_lease_requests.emplace(task_id, *raylet_address);
  ReportWorkerBacklogIfNeeded(scheduling_key);

  // Lease more workers if there are still pending tasks and
  // and we haven't hit the max_pending_lease_requests yet.
  if (scheduling_key_entry.task_queue.size() >
          scheduling_key_entry.pending_lease_requests.size() &&
      scheduling_key_entry.pending_lease_requests.size() <
          kMaxPendingLeaseRequestsPerSchedulingCategory) {
    RequestNewWorkerIfNeeded(scheduling_key);
  }
}

void NormalTaskSubmitter::PushNormalTask(
    const rpc::Address &addr,
    std::shared_ptr<rpc::CoreWorkerClientInterface> client,
    const SchedulingKey &scheduling_key,
    TaskSpecification task_spec,
    const google::protobuf::RepeatedPtrField<rpc::ResourceMapEntry> &assigned_resources) {
  RAY_LOG(DEBUG) << "Pushing task " << task_spec.TaskId() << " to worker "
                 << WorkerID::FromBinary(addr.worker_id()) << " of raylet "
                 << NodeID::FromBinary(addr.raylet_id());
  auto task_id = task_spec.TaskId();
  auto request = std::make_unique<rpc::PushTaskRequest>();
  bool is_actor_creation = task_spec.IsActorCreationTask();

  // NOTE(swang): CopyFrom is needed because if we use Swap here and the task
  // fails, then the task data will be gone when the TaskManager attempts to
  // access the task.
  request->mutable_task_spec()->CopyFrom(task_spec.GetMessage());
  request->mutable_resource_mapping()->CopyFrom(assigned_resources);
  request->set_intended_worker_id(addr.worker_id());
  task_manager_.MarkTaskWaitingForExecution(task_id,
                                            NodeID::FromBinary(addr.raylet_id()),
                                            WorkerID::FromBinary(addr.worker_id()));
  client->PushNormalTask(
      std::move(request),
      [this,
       task_spec = std::move(task_spec),
       task_id,
       is_actor_creation,
       scheduling_key,
       addr,
       assigned_resources](Status status, const rpc::PushTaskReply &reply) {
        bool resubmit_generator = false;
        {
          RAY_LOG(DEBUG) << "Task " << task_id << " finished from worker "
                         << WorkerID::FromBinary(addr.worker_id()) << " of raylet "
                         << NodeID::FromBinary(addr.raylet_id());
          absl::MutexLock lock(&mu_);
          executing_tasks_.erase(task_id);

          resubmit_generator = generators_to_resubmit_.erase(task_id) > 0;

          // Decrement the number of tasks in flight to the worker
          auto &lease_entry = worker_to_lease_entry_[addr];
          RAY_CHECK(lease_entry.is_busy);
          lease_entry.is_busy = false;

          // Decrement the total number of tasks in flight to any worker with the current
          // scheduling_key.
          auto &scheduling_key_entry = scheduling_key_entries_[scheduling_key];
          RAY_CHECK_GE(scheduling_key_entry.active_workers.size(), 1u);
          RAY_CHECK_GE(scheduling_key_entry.num_busy_workers, 1u);
          scheduling_key_entry.num_busy_workers--;

          if (!status.ok()) {
            failed_tasks_pending_failure_cause_.insert(task_id);
            RAY_LOG(DEBUG) << "Getting error from raylet for task " << task_id;
            const ray::rpc::ClientCallback<ray::rpc::GetTaskFailureCauseReply> callback =
                [this, status, task_id, addr](
                    const Status &get_task_failure_cause_reply_status,
                    const rpc::GetTaskFailureCauseReply &get_task_failure_cause_reply) {
                  bool will_retry =
                      HandleGetTaskFailureCause(status,
                                                task_id,
                                                addr,
                                                get_task_failure_cause_reply_status,
                                                get_task_failure_cause_reply);
                  absl::MutexLock lock(&mu_);
                  if (!will_retry) {
                    // Task submission and task cancellation are the only two other code
                    // paths that clean up the cancelled_tasks_ map. If the task is not
                    // retried (aka. it will not go through the task submission path),
                    // we need to remove it from the map here.
                    cancelled_tasks_.erase(task_id);
                  }
                  failed_tasks_pending_failure_cause_.erase(task_id);
                };
            auto &cur_lease_entry = worker_to_lease_entry_[addr];
            RAY_CHECK(cur_lease_entry.lease_client);
            cur_lease_entry.lease_client->GetTaskFailureCause(cur_lease_entry.task_id,
                                                              callback);
          }

          if (!status.ok() || !is_actor_creation || reply.worker_exiting()) {
            bool was_error = !status.ok();
            bool is_worker_exiting = reply.worker_exiting();
            // Successful actor creation leases the worker indefinitely from the raylet.
            OnWorkerIdle(addr,
                         scheduling_key,
                         /*was_error=*/was_error,
                         /*error_detail*/ status.message(),
                         /*worker_exiting=*/is_worker_exiting,
                         assigned_resources);
          }
        }
        if (status.ok()) {
          if (reply.was_cancelled_before_running()) {
            RAY_LOG(DEBUG) << "Task " << task_id
                           << " was cancelled before it started running.";
            task_manager_.FailPendingTask(task_id, rpc::ErrorType::TASK_CANCELLED);
          } else if (resubmit_generator) {
            // If the generator was queued up for resubmission for object recovery,
            // resubmit as long as we get a valid reply.
            task_manager_.MarkGeneratorFailedAndResubmit(task_id);
          } else if (!task_spec.GetMessage().retry_exceptions() ||
                     !reply.is_retryable_error() ||
                     !task_manager_.RetryTaskIfPossible(
                         task_id,
                         gcs::GetRayErrorInfo(rpc::ErrorType::TASK_EXECUTION_EXCEPTION,
                                              reply.task_execution_error()))) {
            task_manager_.CompletePendingTask(
                task_id, reply, addr, reply.is_application_error());
          }
        }
      });
}

bool NormalTaskSubmitter::HandleGetTaskFailureCause(
    const Status &task_execution_status,
    const TaskID &task_id,
    const rpc::Address &addr,
    const Status &get_task_failure_cause_reply_status,
    const rpc::GetTaskFailureCauseReply &get_task_failure_cause_reply) {
  rpc::ErrorType task_error_type = rpc::ErrorType::WORKER_DIED;
  std::unique_ptr<rpc::RayErrorInfo> error_info;
  bool fail_immediately = false;
  if (get_task_failure_cause_reply_status.ok()) {
    RAY_LOG(WARNING) << "Task failure cause for task " << task_id << ": "
                     << ray::gcs::RayErrorInfoToString(
                            get_task_failure_cause_reply.failure_cause())
                     << " fail immedediately: "
                     << get_task_failure_cause_reply.fail_task_immediately();
    if (get_task_failure_cause_reply.has_failure_cause()) {
      task_error_type = get_task_failure_cause_reply.failure_cause().error_type();
      error_info = std::make_unique<rpc::RayErrorInfo>(
          get_task_failure_cause_reply.failure_cause());
      // TODO(clarng): track and append task retry history to the error message.
    }
    fail_immediately = get_task_failure_cause_reply.fail_task_immediately();
  } else {
    RAY_LOG(WARNING) << "Failed to fetch task result with status "
                     << get_task_failure_cause_reply_status.ToString()
                     << " node id: " << NodeID::FromBinary(addr.raylet_id())
                     << " ip: " << addr.ip_address();
    task_error_type = rpc::ErrorType::NODE_DIED;
    std::stringstream buffer;
    buffer << "Task failed due to the node (where this task was running) "
           << " was dead or unavailable.\n\nThe node IP: " << addr.ip_address()
           << ", node ID: " << NodeID::FromBinary(addr.raylet_id()) << "\n\n"
           << "This can happen if the instance where the node was running failed, "
           << "the node was preempted, or raylet crashed unexpectedly "
           << "(e.g., due to OOM) etc.\n\n"
           << "To see node death information, use `ray list nodes --filter \"node_id="
           << NodeID::FromBinary(addr.raylet_id()) << "\"`, "
           << "or check Ray dashboard cluster page, or search the node ID in GCS log, "
           << "or use `ray logs raylet.out -ip " << addr.ip_address() << "`";
    error_info = std::make_unique<rpc::RayErrorInfo>();
    error_info->set_error_message(buffer.str());
    error_info->set_error_type(rpc::ErrorType::NODE_DIED);
  }
  return task_manager_.FailOrRetryPendingTask(task_id,
                                              task_error_type,
                                              &task_execution_status,
                                              error_info.get(),
                                              /*mark_task_object_failed*/ true,
                                              fail_immediately);
}

Status NormalTaskSubmitter::CancelTask(TaskSpecification task_spec,
                                       bool force_kill,
                                       bool recursive) {
  RAY_LOG(INFO) << "Cancelling a task: " << task_spec.TaskId()
                << " force_kill: " << force_kill << " recursive: " << recursive;
  SchedulingKey scheduling_key(
      task_spec.GetSchedulingClass(),
      task_spec.GetDependencyIds(),
      task_spec.IsActorCreationTask() ? task_spec.ActorCreationId() : ActorID::Nil(),
      task_spec.GetRuntimeEnvHash());
  std::shared_ptr<rpc::CoreWorkerClientInterface> client = nullptr;
  {
    absl::MutexLock lock(&mu_);
    auto task_id = task_spec.TaskId();
    generators_to_resubmit_.erase(task_id);

    if (cancelled_tasks_.contains(task_id)) {
      // The task cancel is already in progress. We don't need to do anything.
      return Status::OK();
    }

    task_manager_.MarkTaskCanceled(task_id);
    if (!task_manager_.IsTaskPending(task_id)) {
      // The task is finished or failed so marking the task as cancelled is sufficient.
      return Status::OK();
    }

    auto &scheduling_key_entry = scheduling_key_entries_[scheduling_key];
    auto &scheduling_tasks = scheduling_key_entry.task_queue;
    // This cancels tasks that have completed dependencies and are awaiting
    // a worker lease.
    if (!scheduling_tasks.empty()) {
      for (auto spec = scheduling_tasks.begin(); spec != scheduling_tasks.end(); spec++) {
        if (spec->TaskId() == task_spec.TaskId()) {
          scheduling_tasks.erase(spec);
          CancelWorkerLeaseIfNeeded(scheduling_key);
          task_manager_.FailPendingTask(task_spec.TaskId(),
                                        rpc::ErrorType::TASK_CANCELLED);
          return Status::OK();
        }
      }
    }

    // This will get removed either when the RPC call to cancel is returned
    // or when all dependencies are resolved.
    RAY_CHECK(cancelled_tasks_.emplace(task_spec.TaskId()).second);
    auto rpc_client = executing_tasks_.find(task_spec.TaskId());

    if (rpc_client == executing_tasks_.end()) {
      // This case is reached for tasks that have unresolved dependencies.
      if (failed_tasks_pending_failure_cause_.contains(task_spec.TaskId())) {
        // We are waiting for the task failure cause. Do not fail it here; instead,
        // wait for the cause to come in and then handle it appropriately.
      } else {
        resolver_.CancelDependencyResolution(task_spec.TaskId());
        RAY_UNUSED(task_manager_.FailPendingTask(task_spec.TaskId(),
                                                 rpc::ErrorType::TASK_CANCELLED));
      }
      if (scheduling_key_entry.CanDelete()) {
        // We can safely remove the entry keyed by scheduling_key from the
        // scheduling_key_entries_ hashmap.
        scheduling_key_entries_.erase(scheduling_key);
      }
      return Status::OK();
    }
    // Looks for an RPC handle for the worker executing the task.
    client = core_worker_client_pool_->GetOrConnect(rpc_client->second);
  }

  RAY_CHECK(client != nullptr);
  auto request = rpc::CancelTaskRequest();
  request.set_intended_task_id(task_spec.TaskIdBinary());
  request.set_force_kill(force_kill);
  request.set_recursive(recursive);
  request.set_caller_worker_id(task_spec.CallerWorkerIdBinary());
  client->CancelTask(
      request,
      [this,
       task_spec = std::move(task_spec),
       scheduling_key = std::move(scheduling_key),
       force_kill,
       recursive](const Status &status, const rpc::CancelTaskReply &reply) mutable {
        absl::MutexLock lock(&mu_);
        RAY_LOG(DEBUG) << "CancelTask RPC response received for " << task_spec.TaskId()
                       << " with status " << status.ToString();
        cancelled_tasks_.erase(task_spec.TaskId());

        // Retry is not attempted if !status.ok() because force-kill may kill the worker
        // before the reply is sent.
        if (!status.ok()) {
          RAY_LOG(DEBUG) << "Failed to cancel a task due to " << status.ToString();
          return;
        }

        if (!reply.attempt_succeeded()) {
          if (reply.requested_task_running()) {
            // Retry cancel request if failed.
            if (cancel_retry_timer_.expiry().time_since_epoch() <=
                std::chrono::high_resolution_clock::now().time_since_epoch()) {
              cancel_retry_timer_.expires_after(boost::asio::chrono::milliseconds(
                  RayConfig::instance().cancellation_retry_ms()));
            }
            cancel_retry_timer_.async_wait(boost::bind(&NormalTaskSubmitter::CancelTask,
                                                       this,
                                                       std::move(task_spec),
                                                       force_kill,
                                                       recursive));
          } else {
            RAY_LOG(DEBUG) << "Attempt to cancel task " << task_spec.TaskId()
                           << " in a worker that doesn't have this task.";
          }
        }
      });
  return Status::OK();
}

Status NormalTaskSubmitter::CancelRemoteTask(const ObjectID &object_id,
                                             const rpc::Address &worker_addr,
                                             bool force_kill,
                                             bool recursive) {
  auto client = core_worker_client_pool_->GetOrConnect(worker_addr);
  auto request = rpc::RemoteCancelTaskRequest();
  request.set_force_kill(force_kill);
  request.set_recursive(recursive);
  request.set_remote_object_id(object_id.Binary());
  client->RemoteCancelTask(request, nullptr);
  return Status::OK();
}

bool NormalTaskSubmitter::QueueGeneratorForResubmit(const TaskSpecification &spec) {
  absl::MutexLock lock(&mu_);
  if (cancelled_tasks_.contains(spec.TaskId())) {
    // The user cancelled the task.
    return false;
  }
  generators_to_resubmit_.insert(spec.TaskId());
  return true;
}

}  // namespace core
}  // namespace ray
