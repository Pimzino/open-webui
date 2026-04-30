<script lang="ts">
	import { getContext } from 'svelte';
	import { userCost } from '$lib/stores';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext('i18n');

	export let show = false;

	const formatCurrency = (amount: number, currency: string = 'USD'): string => {
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
</script>

{#if show && $userCost}
	<div class="mx-1 mb-1">
		<div class="w-full px-3 py-1.5 rounded-xl bg-gray-50/50 dark:bg-gray-900/30 text-xs">
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
		</div>
	</div>
{/if}
