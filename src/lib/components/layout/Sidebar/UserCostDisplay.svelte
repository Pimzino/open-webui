<script lang="ts">
	import { getContext } from 'svelte';
	import { getUserCostSummary } from '$lib/apis/analytics';
	import { userCost } from '$lib/stores';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext('i18n');

	export let show = false;

	let loading = false;
	let error: string | null = null;

	const formatCurrency = (amount: number, currency: string = 'USD'): string => {
		// For very small amounts (< $0.01), show up to 6 decimal places
		// For small amounts (< $1), show up to 4 decimal places
		// For larger amounts, show 2 decimal places
		let maxDecimals = 2;
		if (amount > 0 && amount < 0.01) {
			maxDecimals = 6;
		} else if (amount > 0 && amount < 1) {
			maxDecimals = 4;
		}

		return new Intl.NumberFormat('en-US', {
			style: 'currency',
			currency: currency,
			minimumFractionDigits: 2,
			maximumFractionDigits: maxDecimals
		}).format(amount);
	};

	export const loadCostData = async () => {
		if (loading) return;
		loading = true;
		error = null;

		try {
			const data = await getUserCostSummary(localStorage.token);
			if (data) {
				userCost.set({
					today: data.today,
					thisMonth: data.this_month,
					allTime: data.all_time,
					currency: data.currency || 'USD'
				});
			}
		} catch (e) {
			error = e as string;
			console.error('Failed to load cost data:', e);
		} finally {
			loading = false;
		}
	};

	$: if (show && !$userCost && !loading) {
		loadCostData();
	}
</script>

{#if show}
	<div class="mx-1 mb-1">
		<div
			class="w-full px-3 py-1.5 rounded-xl bg-gray-50/50 dark:bg-gray-900/30 text-xs"
		>
			{#if loading}
				<div class="flex items-center justify-center gap-2 text-gray-500 dark:text-gray-400">
					<div class="animate-pulse">{$i18n.t('Loading costs...')}</div>
				</div>
			{:else if error}
				<div class="text-red-500 text-center">{$i18n.t('Unable to load costs')}</div>
			{:else if $userCost}
				<Tooltip
					content={`${$i18n.t('All time')}: ${formatCurrency($userCost.allTime, $userCost.currency)}`}
				>
					<div class="flex items-center justify-between text-gray-600 dark:text-gray-400">
						<div class="flex items-center gap-3">
							<span>
								<span class="font-medium">{$i18n.t('Today')}:</span>
								{formatCurrency($userCost.today, $userCost.currency)}
							</span>
							<span class="text-gray-300 dark:text-gray-600">|</span>
							<span>
								<span class="font-medium">{$i18n.t('Month')}:</span>
								{formatCurrency($userCost.thisMonth, $userCost.currency)}
							</span>
						</div>
					</div>
				</Tooltip>
			{:else}
				<div class="text-gray-500 dark:text-gray-400 text-center">
					{$i18n.t('No cost data')}
				</div>
			{/if}
		</div>
	</div>
{/if}
