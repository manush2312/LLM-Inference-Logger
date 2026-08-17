/**
 * Server-state hooks.
 *
 * TanStack Query owns cache and invalidation so components never hold a
 * duplicate copy of server state. That matters most for the conversation list:
 * sending a message changes a conversation's title and its position in the
 * sidebar, and a hand-rolled cache would need every caller to remember to
 * refresh it.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

export const queryKeys = {
  conversations: ["conversations"] as const,
  conversation: (id: string) => ["conversation", id] as const,
  providers: ["providers"] as const,
};

export function useConversations() {
  return useQuery({
    queryKey: queryKeys.conversations,
    queryFn: api.listConversations,
  });
}

/**
 * The transcript for one conversation.
 *
 * This query is what makes a refreshed page resume: the conversation id lives
 * in the URL, so a cold load refetches the full transcript from the database
 * rather than starting an empty chat.
 */
export function useConversation(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.conversation(id ?? ""),
    queryFn: () => api.getConversation(id!),
    enabled: Boolean(id),
  });
}

export function useProviders() {
  return useQuery({
    queryKey: queryKeys.providers,
    queryFn: api.listProviders,
    // The server's provider set only changes on restart.
    staleTime: Infinity,
  });
}

export function useSendMessage() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: api.sendMessage,
    onSuccess: (response) => {
      // Refetch rather than patch the cache by hand: the server assigns ids,
      // sequence numbers and the auto-generated title, so it is the only
      // thing that knows the conversation's real post-send state.
      void queryClient.invalidateQueries({
        queryKey: queryKeys.conversation(response.conversation_id),
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversations });
    },
  });
}

export function useDeleteConversation() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: api.deleteConversation,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversations });
    },
  });
}
